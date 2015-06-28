import os
import os.path
import re
import time
import queue
import hashlib
import logging
import multiprocessing
from piecrust.chefutil import format_timed, format_timed_scope
from piecrust.processing.base import PipelineContext
from piecrust.processing.records import (
        ProcessorPipelineRecordEntry, TransitionalProcessorPipelineRecord,
        FLAG_PROCESSED)
from piecrust.processing.worker import (
        ProcessingWorkerContext, ProcessingWorkerJob,
        worker_func, get_filtered_processors)


logger = logging.getLogger(__name__)


class _ProcessingContext(object):
    def __init__(self, pool, record, base_dir, mount_info):
        self.pool = pool
        self.record = record
        self.base_dir = base_dir
        self.mount_info = mount_info


class ProcessorPipeline(object):
    def __init__(self, app, out_dir, force=False):
        assert app and out_dir
        self.app = app
        self.out_dir = out_dir
        self.force = force

        tmp_dir = app.sub_cache_dir
        if not tmp_dir:
            import tempfile
            tmp_dir = os.path.join(tempfile.gettempdir(), 'piecrust')
        self.tmp_dir = os.path.join(tmp_dir, 'proc')

        baker_params = app.config.get('baker') or {}

        assets_dirs = baker_params.get('assets_dirs', app.assets_dirs)
        self.mounts = make_mount_infos(assets_dirs, self.app.root_dir)

        self.num_workers = baker_params.get(
                'workers', multiprocessing.cpu_count())

        ignores = baker_params.get('ignore', [])
        ignores += [
                '_cache', '_counter',
                'theme_info.yml',
                '.DS_Store', 'Thumbs.db',
                '.git*', '.hg*', '.svn']
        self.ignore_patterns = make_re(ignores)
        self.force_patterns = make_re(baker_params.get('force', []))

        # Those things are mostly for unit-testing.
        self.enabled_processors = None
        self.additional_processors = None

    def addIgnorePatterns(self, patterns):
        self.ignore_patterns += make_re(patterns)

    def run(self, src_dir_or_file=None, *,
            delete=True, previous_record=None, save_record=True):
        start_time = time.perf_counter()

        # Get the list of processors for this run.
        processors = self.app.plugin_loader.getProcessors()
        if self.enabled_processors is not None:
            logger.debug("Filtering processors to: %s" %
                         self.enabled_processors)
            processors = get_filtered_processors(processors,
                                                 self.enabled_processors)
        if self.additional_processors is not None:
            logger.debug("Adding %s additional processors." %
                         len(self.additional_processors))
            for proc in self.additional_processors:
                self.app.env.registerTimer(proc.__class__.__name__,
                                           raise_if_registered=False)
                proc.initialize(self.app)
                processors.append(proc)

        # Invoke pre-processors.
        pipeline_ctx = PipelineContext(-1, self.app, self.out_dir,
                                       self.tmp_dir, self.force)
        for proc in processors:
            proc.onPipelineStart(pipeline_ctx)

        # Pre-processors can define additional ignore patterns.
        self.ignore_patterns += make_re(
                pipeline_ctx._additional_ignore_patterns)

        # Create the worker pool.
        pool = _WorkerPool()

        # Create the pipeline record.
        record = TransitionalProcessorPipelineRecord()
        record_cache = self.app.cache.getCache('proc')
        record_name = (
                hashlib.md5(self.out_dir.encode('utf8')).hexdigest() +
                '.record')
        if previous_record:
            record.setPrevious(previous_record)
        elif not self.force and record_cache.has(record_name):
            with format_timed_scope(logger, 'loaded previous bake record',
                                    level=logging.DEBUG, colored=False):
                record.loadPrevious(record_cache.getCachePath(record_name))
        logger.debug("Got %d entries in process record." %
                     len(record.previous.entries))
        record.current.success = True
        record.current.processed_count = 0

        # Work!
        def _handler(res):
            entry = record.getCurrentEntry(res.path)
            assert entry is not None
            entry.flags |= res.flags
            entry.proc_tree = res.proc_tree
            entry.rel_outputs = res.rel_outputs
            if entry.flags & FLAG_PROCESSED:
                record.current.processed_count += 1
            if res.errors:
                entry.errors += res.errors
                record.current.success = False

                rel_path = os.path.relpath(res.path, self.app.root_dir)
                logger.error("Errors found in %s:" % rel_path)
                for e in entry.errors:
                    logger.error("  " + e)

        pool = self._createWorkerPool()
        expected_result_count = self._process(src_dir_or_file, pool, record)
        self._waitOnWorkerPool(pool, expected_result_count, _handler)
        self._terminateWorkerPool(pool)

        # Get timing information from the workers.
        record.current.timers = {}
        for i in range(len(pool.workers)):
            try:
                timers = pool.results.get(True, 0.1)
            except queue.Empty:
                logger.error("Didn't get timing information from all workers.")
                break

            worker_name = 'PipelineWorker_%d' % i
            record.current.timers[worker_name] = {}
            for name, val in timers['data'].items():
                main_val = record.current.timers.setdefault(name, 0)
                record.current.timers[name] = main_val + val
                record.current.timers[worker_name][name] = val

        # Invoke post-processors.
        pipeline_ctx.record = record.current
        for proc in processors:
            proc.onPipelineEnd(pipeline_ctx)

        # Handle deletions.
        if delete:
            for path, reason in record.getDeletions():
                logger.debug("Removing '%s': %s" % (path, reason))
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
                logger.info('[delete] %s' % path)

        # Finalize the process record.
        record.current.process_time = time.time()
        record.current.out_dir = self.out_dir
        record.collapseRecords()

        # Save the process record.
        if save_record:
            with format_timed_scope(logger, 'saved bake record',
                                    level=logging.DEBUG, colored=False):
                record.saveCurrent(record_cache.getCachePath(record_name))

        logger.info(format_timed(
                start_time,
                "processed %d assets." % record.current.processed_count))

        return record.detach()

    def _process(self, src_dir_or_file, pool, record):
        expected_result_count = 0

        if src_dir_or_file is not None:
            # Process only the given path.
            # Find out what mount point this is in.
            for name, info in self.mounts.items():
                path = info['path']
                if src_dir_or_file[:len(path)] == path:
                    base_dir = path
                    mount_info = info
                    break
            else:
                known_roots = [i['path'] for i in self.mounts.values()]
                raise Exception("Input path '%s' is not part of any known "
                                "mount point: %s" %
                                (src_dir_or_file, known_roots))

            ctx = _ProcessingContext(pool, record, base_dir, mount_info)
            logger.debug("Initiating processing pipeline on: %s" %
                         src_dir_or_file)
            if os.path.isdir(src_dir_or_file):
                expected_result_count = self._processDirectory(
                        ctx, src_dir_or_file)
            elif os.path.isfile(src_dir_or_file):
                self._processFile(ctx, src_dir_or_file)
                expected_result_count = 1

        else:
            # Process everything.
            for name, info in self.mounts.items():
                path = info['path']
                ctx = _ProcessingContext(pool, record, path, info)
                logger.debug("Initiating processing pipeline on: %s" % path)
                expected_result_count = self._processDirectory(ctx, path)

        return expected_result_count

    def _processDirectory(self, ctx, start_dir):
        queued_count = 0
        for dirpath, dirnames, filenames in os.walk(start_dir):
            rel_dirpath = os.path.relpath(dirpath, start_dir)
            dirnames[:] = [d for d in dirnames
                           if not re_matchany(
                               d, self.ignore_patterns, rel_dirpath)]

            for filename in filenames:
                if re_matchany(filename, self.ignore_patterns, rel_dirpath):
                    continue
                self._processFile(ctx, os.path.join(dirpath, filename))
                queued_count += 1
        return queued_count

    def _processFile(self, ctx, path):
        # TODO: handle overrides between mount-points.

        entry = ProcessorPipelineRecordEntry(path)
        ctx.record.addEntry(entry)

        previous_entry = ctx.record.getPreviousEntry(path)
        force_this = (self.force or previous_entry is None or
                      not previous_entry.was_processed_successfully)

        job = ProcessingWorkerJob(ctx.base_dir, ctx.mount_info, path,
                                  force=force_this)

        logger.debug("Queuing: %s" % path)
        ctx.pool.queue.put_nowait(job)

    def _createWorkerPool(self):
        pool = _WorkerPool()
        for i in range(self.num_workers):
            ctx = ProcessingWorkerContext(
                    self.app.root_dir, self.out_dir, self.tmp_dir,
                    pool.queue, pool.results, pool.abort_event,
                    self.force, self.app.debug)
            ctx.enabled_processors = self.enabled_processors
            ctx.additional_processors = self.additional_processors
            w = multiprocessing.Process(
                    name='PipelineWorker_%d' % i,
                    target=worker_func, args=(i, ctx))
            w.start()
            pool.workers.append(w)
        return pool

    def _waitOnWorkerPool(self, pool, expected_result_count, result_handler):
        abort_with_exception = None
        try:
            got_count = 0
            while got_count < expected_result_count:
                try:
                    res = pool.results.get(True, 10)
                except queue.Empty:
                    logger.error(
                            "Got %d results, expected %d, and timed-out "
                            "for 10 seconds. A worker might be stuck?" %
                            (got_count, expected_result_count))
                    abort_with_exception = Exception("Worker time-out.")
                    break

                if isinstance(res, dict) and res.get('type') == 'error':
                    abort_with_exception = Exception(
                            'Worker critical error:\n' +
                            '\n'.join(res['messages']))
                    break

                got_count += 1
                result_handler(res)
        except KeyboardInterrupt as kiex:
            logger.warning("Bake aborted by user... "
                           "waiting for workers to stop.")
            abort_with_exception = kiex

        if abort_with_exception:
            pool.abort_event.set()
            for w in pool.workers:
                w.join(2)
            raise abort_with_exception

    def _terminateWorkerPool(self, pool):
        pool.abort_event.set()
        for w in pool.workers:
            w.join()


class _WorkerPool(object):
    def __init__(self):
        self.queue = multiprocessing.JoinableQueue()
        self.results = multiprocessing.Queue()
        self.abort_event = multiprocessing.Event()
        self.workers = []


def make_mount_infos(mounts, root_dir):
    if isinstance(mounts, list):
        mounts = {m: {} for m in mounts}

    for name, info in mounts.items():
        if not isinstance(info, dict):
            raise Exception("Asset directory info for '%s' is not a "
                            "dictionary." % name)
        info.setdefault('processors', 'all -uglifyjs -cleancss')
        info['path'] = os.path.join(root_dir, name)

    return mounts


def make_re(patterns):
    re_patterns = []
    for pat in patterns:
        if pat[0] == '/' and pat[-1] == '/' and len(pat) > 2:
            re_patterns.append(pat[1:-1])
        else:
            escaped_pat = (
                    re.escape(pat)
                    .replace(r'\*', r'[^/\\]*')
                    .replace(r'\?', r'[^/\\]'))
            re_patterns.append(escaped_pat)
    return [re.compile(p) for p in re_patterns]


def re_matchany(filename, patterns, dirname=None):
    if dirname and dirname != '.':
        filename = os.path.join(dirname, filename)

    # skip patterns use a forward slash regardless of the platform.
    filename = filename.replace('\\', '/')
    for pattern in patterns:
        if pattern.search(filename):
            return True
    return False

