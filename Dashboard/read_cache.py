"""Short-lived, scope-keyed results and one computation per identical request."""
import collections
import threading
import time


class QueryBusy(Exception):
    pass


class ReadCache:
    def __init__(self, ttl=4, maximum=24, wait=12):
        self.ttl,self.maximum,self.wait=ttl,maximum,wait
        self.lock=threading.Lock()
        self.results=collections.OrderedDict()
        self.pending={}

    def run(self,key,compute):
        with self.lock:
            cached=self.results.get(key)
            if cached and time.monotonic()-cached[0]<self.ttl:
                self.results.move_to_end(key)
                return cached[1]
            job=self.pending.get(key)
            owner=job is None
            if owner:
                job={'done':threading.Event()}
                self.pending[key]=job
        if not owner:
            if not job['done'].wait(self.wait):raise QueryBusy('等待同一查询完成')
            if 'error' in job:raise job['error']
            return job['value']
        try:
            value=compute()
            with self.lock:
                self.results[key]=(time.monotonic(),value)
                self.results.move_to_end(key)
                while len(self.results)>self.maximum:self.results.popitem(last=False)
                job['value']=value
            return value
        except Exception as exc:
            with self.lock:job['error']=exc
            raise
        finally:
            with self.lock:
                self.pending.pop(key,None)
                job['done'].set()
