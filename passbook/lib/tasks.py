"""Monitored tasks"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional

from celery import Task
from django.core.cache import cache


class TaskResultStatus(Enum):
    """Possible states of tasks"""

    SUCCESSFUL = 1
    WARNING = 2
    ERROR = 4


@dataclass
class TaskResult:
    """Result of a task run, this class is created by the task itself
    and used by self.set_status"""

    status: TaskResultStatus

    messages: List[str] = field(default_factory=list)

    error: Optional[Exception] = field(default=None)

    # Optional UID used in cache for tasks that run in different instances
    uid: Optional[str] = field(default=None)


@dataclass
class TaskInfo:
    """Info about a task run"""

    task_name: str
    finish_timestamp: datetime

    result: TaskResult

    task_description: Optional[str] = field(default=None)

    def save(self):
        """Save task into cache"""
        key = f"task_{self.task_name}"
        if self.result.uid:
            key += f"_{self.result.uid}"
            self.task_name += f"_{self.result.uid}"
        cache.set(key, self)


class MonitoredTask(Task):
    """Task which can save its state to the cache"""

    _result: TaskResult

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._result = TaskResult(status=TaskResultStatus.ERROR, messages=[])

    def set_status(self, result: TaskResult):
        """Set result for current run, will overwrite previous result."""
        self._result = result

    # pylint: disable=too-many-arguments
    def after_return(self, status, retval, task_id, args, kwargs, einfo):
        TaskInfo(
            task_name=self.__name__,
            task_description=self.__doc__,
            finish_timestamp=datetime.now(),
            result=self._result,
        ).save()
        return super().after_return(status, retval, task_id, args, kwargs, einfo=einfo)

    # pylint: disable=too-many-arguments
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        TaskInfo(
            task_name=self.__name__,
            task_description=self.__doc__,
            finish_timestamp=datetime.now(),
            result=self._result,
        ).save()
        return super().on_failure(exc, task_id, args, kwargs, einfo=einfo)

    def run(self, *args, **kwargs):
        raise NotImplementedError
