"""Cooperative cancellation confined to the active delivery thread."""
import threading
from contextlib import contextmanager

state=threading.local()

class DeliveryStopped(RuntimeError):
    pass

def check_stop():
    event=getattr(state,'event',None)
    if event and event.is_set():raise DeliveryStopped('用户请求终止任务；已提交客户端的文件不会撤回')

@contextmanager
def cancellable(event):
    state.event=event
    try:yield
    finally:state.event=None
