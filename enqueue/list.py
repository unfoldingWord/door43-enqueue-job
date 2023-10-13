import redis
from rq import Queue
import sys
from functools import reduce

r = redis.Redis(port=6379, db=0)

def list():
    queues = Queue.all(connection=r)
    queues.sort(key=lambda x: x.name, reverse=False)
    for queue in queues:
        print("-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-")
        print(f"{queue.name} Registries:")

        print(f"Queued Jobs:")
        n = len(queue.get_jobs())
        for job in queue.get_jobs():
            if job:
                print(f"{job.id}")
        print(f"Total {n} Jobs in queue\n")

        print(f"Scheduled Jobs:")
        n = len(queue.scheduled_job_registry.get_job_ids())
        for id in queue.scheduled_job_registry.get_job_ids():
            job = queue.fetch_job(id)
            if job:
                print(f"{id}: {job.is_scheduled}")
        print(f"Total {n} Jobs scheduled\n")

        print(f"Started Jobs:")
        n = len(queue.started_job_registry.get_job_ids())
        for id in queue.started_job_registry.get_job_ids():
            job = queue.fetch_job(id)
            if job:
                print(f"{id}: {job.is_started}")
        print(f"Total {n} Jobs started\n")

        print(f"Finished Jobs:")
        n = len(queue.finished_job_registry.get_job_ids())
        for id in queue.finished_job_registry.get_job_ids():
            job = queue.fetch_job(id)
            if job:
                print(f"{id}: {job.is_finished}")
        print(f"Total {n} Jobs finished\n")

        print(f"Canceled Jobs:")
        n = len(queue.canceled_job_registry.get_job_ids())
        for id in queue.canceled_job_registry.get_job_ids():
            job = queue.fetch_job(id)
            if job:
                print(f"{id}: {job.is_canceled}")
        print(f"Total {n} Jobs canceled\n")

        print(f"Failed Jobs:")
        n = len(queue.failed_job_registry.get_job_ids())
        for id in queue.failed_job_registry.get_job_ids():
            job = queue.fetch_job(id)
            if job:
                print(f"{id}: {job.is_failed}")
        print(f"Total {n} Jobs failed\n")

        print(f"ALL Jobs:")
        job_ids = queue.scheduled_job_registry.get_job_ids() + queue.get_job_ids() + queue.started_job_registry.get_job_ids() + queue.finished_job_registry.get_job_ids() + queue.deferred_job_registry.get_job_ids() + queue.canceled_job_registry.get_job_ids() + queue.failed_job_registry.get_job_ids()
        n = len(job_ids)
        for id in job_ids:
            job = queue.fetch_job(id)
            if job:
                print(id)
        print(f"Total {n} Jobs\n")


def getJob(job_id):
    djh_queue = Queue("door43-job-handler", connection=r)
    tjh_queue = Queue("tx-job-handler", connection=r)
    res = djh_queue.fetch_job(job_id)
    if res == None:
        res = tjh_queue.fetch_job(job_id)
    print(res.args, file=sys.stderr)
    if not res.result:
        return f'<center><br /><br /><h3>The job is still pending</h3><br /><br />ID:{job_id}<br />Queued at: {res.enqueued_at}<br />Status: {res._status}</center><b>{res.args[1]}</b>'
    return f'<center><br /><br /><img src="{res.result}" height="200px"><br /><br />ID:{job_id}<br />Queued at: {res.enqueued_at}<br />Finished at: {res.ended_at}</center><b>{res.args[1]}</b>'


if __name__ == "__main__":
    list()