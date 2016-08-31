import logging
import os
import time
import uuid
from threading import Lock

from golem.core.statskeeper import IntStatsKeeper
from golem.docker.machine.machine_manager import DockerMachineManager
from golem.docker.task_thread import DockerTaskThread
from golem.manager.nodestatesnapshot import TaskChunkStateSnapshot
from golem.resource.resourcesmanager import ResourcesManager
from golem.resource.dirmanager import DirManager
from golem.task.taskthread import TaskThread
from golem.vm.vm import PythonProcVM, PythonTestVM


logger = logging.getLogger(__name__)


class CompStats(object):
    def __init__(self):
        self.computed_tasks = 0
        self.tasks_with_timeout = 0
        self.tasks_with_errors = 0


class TaskComputer(object):
    """ TaskComputer is responsible for task computations that take place in Golem application. Tasks are started
    in separate threads.
    """

    lock = Lock()
    dir_lock = Lock()

    def __init__(self, node_name, task_server, use_docker_machine_manager=True):
        """ Create new task computer instance
        :param node_name:
        :param task_server:
        :return:
        """
        self.node_name = node_name
        self.task_server = task_server
        self.waiting_for_task = None
        self.counting_task = False
        self.task_requested = False
        self.runnable = True
        self.listeners = []
        self.current_computations = []
        self.last_task_request = time.time()

        self.waiting_ttl = 0
        self.last_checking = time.time()

        self.dir_manager = None
        self.resource_manager = None
        self.task_request_frequency = None
        self.use_waiting_ttl = None
        self.waiting_for_task_timeout = None
        self.waiting_for_task_session_timeout = None

        self.docker_manager = DockerMachineManager()
        self.use_docker_machine_manager = use_docker_machine_manager
        self.change_config(task_server.config_desc,
                           in_background=False)

        self.stats = IntStatsKeeper(CompStats)

        self.assigned_subtasks = {}
        self.task_to_subtask_mapping = {}
        self.max_assigned_tasks = 1

        self.delta = None
        self.task_timeout = None
        self.last_task_timeout_checking = None
        self.support_direct_computation = False
        self.compute_tasks = task_server.config_desc.accept_tasks

    def task_given(self, ctd, subtask_timeout):
        if ctd.subtask_id not in self.assigned_subtasks:
            self.wait(ttl=self.waiting_for_task_timeout)
            self.assigned_subtasks[ctd.subtask_id] = ctd
            self.assigned_subtasks[ctd.subtask_id].timeout = subtask_timeout
            self.task_to_subtask_mapping[ctd.task_id] = ctd.subtask_id
            self.__request_resource(ctd.task_id, self.resource_manager.get_resource_header(ctd.task_id),
                                    ctd.return_address, ctd.return_port, ctd.key_id, ctd.task_owner)
            return True
        else:
            return False

    def resource_given(self, task_id):
        if task_id in self.task_to_subtask_mapping:
            subtask_id = self.task_to_subtask_mapping[task_id]
            if subtask_id in self.assigned_subtasks:
                subtask = self.assigned_subtasks[subtask_id]
                self.__compute_task(subtask_id, subtask.docker_images,
                                    subtask.src_code, subtask.extra_data,
                                    subtask.short_description, subtask.timeout)
                return True
            else:
                return False

    def task_resource_collected(self, task_id, unpack_delta=True):
        if task_id in self.task_to_subtask_mapping:
            subtask_id = self.task_to_subtask_mapping[task_id]
            if subtask_id in self.assigned_subtasks:
                subtask = self.assigned_subtasks[subtask_id]
                if unpack_delta:
                    self.task_server.unpack_delta(self.dir_manager.get_task_resource_dir(task_id), self.delta, task_id)
                self.delta = None
                self.task_timeout = subtask.timeout
                self.last_task_timeout_checking = time.time()
                self.__compute_task(subtask_id, self.assigned_subtasks[subtask_id].docker_images,
                                    self.assigned_subtasks[subtask_id].src_code,
                                    self.assigned_subtasks[subtask_id].extra_data,
                                    self.assigned_subtasks[subtask_id].short_description,
                                    self.assigned_subtasks[subtask_id].timeout)

                return True
            return False

    def task_resource_failure(self, task_id, reason):
        if task_id in self.task_to_subtask_mapping:
            subtask_id = self.task_to_subtask_mapping.pop(task_id)
            if subtask_id in self.assigned_subtasks:
                subtask = self.assigned_subtasks.pop(subtask_id)
                self.task_server.send_task_failed(subtask_id, subtask.task_id,
                                                  'Error downloading resources: {}'.format(reason),
                                                  subtask.return_address, subtask.return_port, subtask.key_id,
                                                  subtask.task_owner, self.node_name)
            self.session_closed()

    def wait_for_resources(self, task_id, delta):
        if task_id in self.task_to_subtask_mapping:
            subtask_id = self.task_to_subtask_mapping[task_id]
            if subtask_id in self.assigned_subtasks:
                self.delta = delta

    def task_request_rejected(self, task_id, reason):
        logger.warning("Task {} request rejected: {}".format(task_id, reason))

    def resource_request_rejected(self, subtask_id, reason):
        logger.warning("Task {} resource request rejected: {}".format(subtask_id, reason))
        self.assigned_subtasks.pop(subtask_id, None)
        self.reset()

    def task_computed(self, task_thread):
        if task_thread.end_time is None:
            task_thread.end_time = time.time()

        with self.lock:
            if task_thread in self.current_computations:
                self.current_computations.remove(task_thread)

        time_ = task_thread.end_time - task_thread.start_time
        subtask_id = task_thread.subtask_id
        subtask = self.assigned_subtasks.pop(subtask_id, None)

        if not subtask:
            logger.error("No subtask with id {}".format(subtask_id))
            return

        if task_thread.error or task_thread.error_msg:
            if "Task timed out" in task_thread.error_msg:
                self.stats.increase_stat('tasks_with_timeout')
            else:
                self.stats.increase_stat('tasks_with_errors')
            self.task_server.send_task_failed(subtask_id, subtask.task_id, task_thread.error_msg,
                                              subtask.return_address, subtask.return_port, subtask.key_id,
                                              subtask.task_owner, self.node_name)
        elif task_thread.result and 'data' in task_thread.result and 'result_type' in task_thread.result:
            logger.info("Task {} computed".format(subtask_id))
            self.stats.increase_stat('computed_tasks')
            self.task_server.send_results(subtask_id, subtask.task_id, task_thread.result, time_,
                                          subtask.return_address, subtask.return_port, subtask.key_id,
                                          subtask.task_owner, self.node_name)
        else:
            self.stats.increase_stat('tasks_with_errors')
            self.task_server.send_task_failed(subtask_id, subtask.task_id, "Wrong result format",
                                              subtask.return_address, subtask.return_port, subtask.key_id,
                                              subtask.task_owner, self.node_name)
        self.counting_task = None

    def run(self):
        if self.counting_task:
            for task_thread in self.current_computations:
                task_thread.check_timeout()
        elif self.compute_tasks and self.runnable:
            if not self.waiting_for_task:
                if time.time() - self.last_task_request > self.task_request_frequency:
                    if len(self.current_computations) == 0:
                        self.__request_task()
            elif self.use_waiting_ttl:
                time_ = time.time()
                self.waiting_ttl -= time_ - self.last_checking
                self.last_checking = time_
                if self.waiting_ttl < 0:
                    self.reset()

    def get_progresses(self):
        ret = {}
        for c in self.current_computations:
            tcss = TaskChunkStateSnapshot(c.get_subtask_id(), 0.0, 0.0, c.get_progress(),
                                          c.get_task_short_desc())  # FIXME: cpu power and estimated time left
            ret[c.subtask_id] = tcss

        return ret

    def change_config(self, config_desc, in_background=True):
        self.dir_manager = DirManager(self.task_server.get_task_computer_root())
        self.resource_manager = ResourcesManager(self.dir_manager, self)
        self.task_request_frequency = config_desc.task_request_interval
        self.waiting_for_task_timeout = config_desc.waiting_for_task_timeout
        self.waiting_for_task_session_timeout = config_desc.waiting_for_task_session_timeout
        self.compute_tasks = config_desc.accept_tasks
        self.change_docker_config(config_desc, in_background)

    def change_docker_config(self, config_desc, in_background=True):
        dm = self.docker_manager
        dm.build_config(config_desc)

        if dm.docker_machine_available and self.use_docker_machine_manager:

            self.toggle_config_dialog(True)

            def status_callback():
                return self.counting_task

            def done_callback():
                logger.debug("Resuming new task computation")
                self.toggle_config_dialog(False)
                self.runnable = True

            self.runnable = False
            dm.update_config(status_callback,
                             done_callback,
                             in_background)

    def register_listener(self, listener):
        self.listeners.append(listener)

    def toggle_config_dialog(self, on=True):
        for l in self.listeners:
            l.toggle_config_dialog(on)

    def session_timeout(self):
        self.session_closed()

    def session_closed(self):
        if not self.counting_task:
            self.reset()

    def wait(self, wait=True, ttl=None):
        self.use_waiting_ttl = wait
        if ttl is None:
            self.waiting_ttl = self.waiting_for_task_session_timeout
        else:
            self.waiting_ttl = ttl

    def reset(self, counting_task=False):
        self.counting_task = counting_task
        self.use_waiting_ttl = False
        self.task_requested = False
        self.waiting_for_task = None
        self.waiting_ttl = 0

    def __request_task(self):
        with self.lock:
            perform_request = not self.waiting_for_task and not self.counting_task

        if perform_request:
            now = time.time()
            self.wait()
            self.last_checking = now
            self.last_task_request = now
            self.waiting_for_task = self.task_server.request_task()

    def __request_resource(self, task_id, resource_header, return_address, return_port, key_id, task_owner):
        self.last_checking = time.time()
        self.wait(ttl=self.waiting_for_task_timeout)
        self.waiting_for_task = self.task_server.request_resource(task_id, resource_header, return_address, return_port,
                                                                  key_id,
                                                                  task_owner)

    def __compute_task(self, subtask_id, docker_images,
                       src_code, extra_data, short_desc, task_timeout):

        self.reset(counting_task=True)

        task_id = self.assigned_subtasks[subtask_id].task_id
        working_dir = self.assigned_subtasks[subtask_id].working_directory
        unique_str = str(uuid.uuid4())

        with self.dir_lock:
            resource_dir = self.resource_manager.get_resource_dir(task_id)
            temp_dir = os.path.join(self.resource_manager.get_temporary_dir(task_id), unique_str)
            # self.dir_manager.clear_temporary(task_id)

            if not os.path.exists(temp_dir):
                os.makedirs(temp_dir)

        if docker_images:
            tt = DockerTaskThread(self, subtask_id, docker_images, working_dir,
                                  src_code, extra_data, short_desc,
                                  resource_dir, temp_dir, task_timeout)
        elif self.support_direct_computation:
            tt = PyTaskThread(self, subtask_id, working_dir, src_code,
                              extra_data, short_desc, resource_dir, temp_dir,
                              task_timeout)
        else:
            logger.error("Cannot run PyTaskThread in this version")
            subtask = self.assigned_subtasks.pop(subtask_id)
            self.task_server.send_task_failed(subtask_id, subtask.task_id, "Host direct task not supported",
                                              subtask.return_address, subtask.return_port, subtask.key_id,
                                              subtask.task_owner, self.node_name)
            self.counting_task = None
            return

        tt.setDaemon(True)
        self.current_computations.append(tt)
        tt.start()

    def quit(self):
        for t in self.current_computations:
            t.end_comp()


class AssignedSubTask(object):
    def __init__(self, src_code, extra_data, short_desc, owner_address, owner_port):
        self.src_code = src_code
        self.extra_data = extra_data
        self.short_desc = short_desc
        self.owner_address = owner_address
        self.owner_port = owner_port


class PyTaskThread(TaskThread):
    def __init__(self, task_computer, subtask_id, working_directory, src_code, extra_data, short_desc, res_path,
                 tmp_path, timeout):
        super(PyTaskThread, self).__init__(task_computer, subtask_id, working_directory, src_code, extra_data,
                                           short_desc, res_path, tmp_path, timeout)
        self.vm = PythonProcVM()


class PyTestTaskThread(PyTaskThread):
    def __init__(self, task_computer, subtask_id, working_directory, src_code, extra_data, short_desc, res_path,
                 tmp_path, timeout):
        super(PyTestTaskThread, self).__init__(task_computer, subtask_id, working_directory, src_code, extra_data,
                                               short_desc, res_path, tmp_path, timeout)
        self.vm = PythonTestVM()

