# Adapted by RJH June 2018 from fork of https://github.com/lscsoft/webhook-queue (Public Domain / unlicense.org)
#   The main change was to add some vetting of the json payload before allowing the job to be queued.
#   Updated Sept 2018 to add callback service

# TODO: We don't currently have any way to clear the failed queue

# Python imports
from os import getenv, environ
import sys
from datetime import datetime, timedelta
import logging
import boto3
import watchtower
from functools import reduce
import json

# Library (PyPI) imports
from flask import Flask, request, jsonify
from flask_cors import CORS
# NOTE: We use StrictRedis() because we don't need the backwards compatibility of Redis()
from redis import StrictRedis
from rq import Queue, Worker
from rq.registry import FailedJobRegistry
from rq.command import send_stop_job_command
from statsd import StatsClient # Graphite front-end


# Local imports
from check_posted_payload import check_posted_payload, check_posted_callback_payload

DEV_PREFIX = 'dev-'


LOGGING_NAME = 'door43_enqueue_job' # Used for logging
DOOR43_JOB_HANDLER_QUEUE_NAME = 'door43_job_handler' # The main queue name for generating HTML, PDF, etc. files (and graphite name) -- MUST match setup.py in door43-job-handler. Will get prefixed for dev
DOOR43_CATALOG_JOB_HANDLER_QUEUE_NAME = 'door43_catalog_job_handler' # The catalog backport queue name -- MUST match setup.py in door43-catalog-job-handler. Will get prefixed for devCALLBACK_SUFFIX = '_callback' # The callback prefix for the DJH_NAME to handle deploy files
DOOR43_JOB_HANDLER_CALLBACK_QUEUE_NAME = 'door43_job_handler_callback'

# NOTE: The following strings if not empty, MUST have a trailing slash but NOT a leading one.
#WEBHOOK_URL_SEGMENT = 'client/webhook/'
WEBHOOK_URL_SEGMENT = '' # Leaving this blank will cause the service to run at '/'
CALLBACK_URL_SEGMENT = WEBHOOK_URL_SEGMENT + 'tx-callback/'

# Look at relevant environment variables
PREFIX = getenv('QUEUE_PREFIX', '') # Gets (optional) QUEUE_PREFIX environment variable -- set to 'dev-' for development
PREFIXED_LOGGING_NAME = PREFIX + LOGGING_NAME
PREFIXED_DOOR43_JOB_HANDLER_QUEUE_NAME = PREFIX + DOOR43_JOB_HANDLER_QUEUE_NAME
PREFIXED_DOOR43_JOB_HANDLER_CALLBACK_QUEUE_NAME = PREFIX + DOOR43_JOB_HANDLER_CALLBACK_QUEUE_NAME
PREFIXED_DOOR43_CATALOG_JOB_HANDLER_QUEUE_NAME = PREFIX + DOOR43_CATALOG_JOB_HANDLER_QUEUE_NAME

# NOTE: Large lexicons like UGL and UAHL seem to be the longest-running jobs
WEBHOOK_TIMEOUT = '900s' if PREFIX else '600s' # Then a running job (taken out of the queue) will be considered to have failed
    # NOTE: This is only the time until webhook.py returns after preprocessing and submitting the job
    #           -- the actual conversion jobs might still be running.
    # RJH: 480s fails on UHB 76,000+ link checks for my slow internet (took 361s)
    # RJH: 480s fails on UGNT 33,000+ link checks for my slow internet (took 596s)
CALLBACK_TIMEOUT = '1200s' if PREFIX else '600s' # Then a running callback job (taken out of the queue) will be considered to have failed
    # RJH: 480s fails on UGL upload for my slow internet (600s fails even on mini UGL upload!!!)

MINUTES_TO_WAIT = 10
try:
    MINUTES_TO_WAIT = int(getenv('MINUTES_TO_WAIT', '10'))
except:
    pass

# Get the redis URL from the environment, otherwise use a local test instance
REDIS_HOSTNAME = getenv('REDIS_HOSTNAME', 'redis')
# Use this to detect test mode (coz logs will go into a separate AWS CloudWatch stream)
DEBUG_MODE_FLAG = getenv('DEBUG_MODE', 'False').lower() not in ('false', '0', 'f', '')
TEST_STRING = " (TEST)" if DEBUG_MODE_FLAG else ""

# global variables
echo_prodn_to_dev_flag = False
logger = logging.getLogger(PREFIXED_LOGGING_NAME)
sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s: %(message)s'))
logger.addHandler(sh)
aws_access_key_id = environ['AWS_ACCESS_KEY_ID']
aws_secret_access_key = environ['AWS_SECRET_ACCESS_KEY']
boto3_client = boto3.client("logs", aws_access_key_id=aws_access_key_id,
                        aws_secret_access_key=aws_secret_access_key,
                        region_name='us-west-2')
test_mode_flag = getenv('TEST_MODE', '')
travis_flag = getenv('TRAVIS_BRANCH', '')
log_group_name = f"{'' if test_mode_flag or travis_flag else PREFIX}tX" \
                 f"{'_DEBUG' if DEBUG_MODE_FLAG else ''}" \
                 f"{'_TEST' if test_mode_flag else ''}" \
                 f"{'_TravisCI' if travis_flag else ''}"
watchtower_log_handler = watchtower.CloudWatchLogHandler(boto3_client=boto3_client,
                                                log_group_name=log_group_name,
                                                stream_name=PREFIXED_LOGGING_NAME)
logger.addHandler(watchtower_log_handler)
logger.debug(f"Logging to AWS CloudWatch group '{log_group_name}' using key '…{aws_access_key_id[-2:]}'.")
# Enable DEBUG logging for dev- instances (but less logging for production)
logger.setLevel(logging.DEBUG if PREFIX else logging.INFO)


# Setup queue variables
QUEUE_NAME_SUFFIX = '' # Used to switch to a different queue, e.g., '_1'
if PREFIX not in ('', DEV_PREFIX):
    logger.critical(f"Unexpected prefix: '{PREFIX}' — expected '' or '{DEV_PREFIX}'")
if PREFIX: # don't use production queue
    djh_adjusted_webhook_queue_name = PREFIXED_DOOR43_JOB_HANDLER_QUEUE_NAME + QUEUE_NAME_SUFFIX # Will become our main queue name
    djh_adjusted_callback_queue_name = PREFIXED_DOOR43_JOB_HANDLER_CALLBACK_QUEUE_NAME + QUEUE_NAME_SUFFIX
    dcjh_adjusted_queue_name = PREFIXED_DOOR43_CATALOG_JOB_HANDLER_QUEUE_NAME + QUEUE_NAME_SUFFIX # Will become the catalog handler queue name
else: # production code
    djh_adjusted_webhook_queue_name = DOOR43_JOB_HANDLER_QUEUE_NAME + QUEUE_NAME_SUFFIX # Will become our main queue name
    djh_adjusted_callback_queue_name = DOOR43_JOB_HANDLER_CALLBACK_QUEUE_NAME + QUEUE_NAME_SUFFIX
    dcjh_adjusted_queue_name = DOOR43_CATALOG_JOB_HANDLER_QUEUE_NAME + QUEUE_NAME_SUFFIX # Will become the catalog handler queue name
# NOTE: The prefixed version must also listen at a different port (specified in gunicorn run command)


prefix_string = f" ({PREFIX})" if PREFIX else ""
logger.info(f"enqueueMain.py{prefix_string}{TEST_STRING} running on Python v{sys.version}")


# Connect to Redis now so it fails at import time if no Redis instance available
logger.info(f"redis_hostname is '{REDIS_HOSTNAME}'")
logger.debug(f"{PREFIXED_LOGGING_NAME} connecting to Redis…")
redis_connection = StrictRedis(host=REDIS_HOSTNAME)
logger.debug("Getting total worker count in order to verify working Redis connection…")
total_rq_worker_count = Worker.count(connection=redis_connection)
logger.debug(f"Total rq workers = {total_rq_worker_count}")


# Get the Graphite URL from the environment, otherwise use a local test instance
graphite_url = getenv('GRAPHITE_HOSTNAME', 'localhost')
logger.info(f"graphite_url is '{graphite_url}'")
stats_prefix = f"door43.{'dev' if PREFIX else 'prod'}"
enqueue_job_stats_prefix = f"{stats_prefix}.enqueue-job"
enqueue_callback_job_stats_prefix = f"{stats_prefix}.enqueue-callback-job"
enqueue_catalog_job_stats_prefix = f"{stats_prefix}.enqueue-catalog-job"
stats_client = StatsClient(host=graphite_url, port=8125)


app = Flask(__name__)
if PREFIX:
    CORS(app, resources={r"/*": {"origins": "*", "allow_headers": "*", "expose_headers": "*"}})
# Not sure that we need this Flask logging
# app.logger.addHandler(watchtower_log_handler)
# logging.getLogger('werkzeug').addHandler(watchtower_log_handler)
logger.info(f"{djh_adjusted_webhook_queue_name}, {djh_adjusted_callback_queue_name} and {dcjh_adjusted_queue_name} are up and ready to go")


def handle_failed_queue(queue_name:str) -> int:
    """
    Go through the failed queue, and see how many entries originated from our queue.

    Of those, permanently delete any that are older than two weeks old.
    """
    failed_queue = Queue('failed', connection=redis_connection)
    len_failed_queue = len(failed_queue)
    if len_failed_queue:
        logger.debug(f"There are {len_failed_queue} total jobs in failed queue")

    len_failed_queue = 0
    for failed_job in failed_queue.jobs.copy():
        if failed_job.origin == queue_name:
            failed_duration = datetime.utcnow() - failed_job.enqueued_at
            if failed_duration >= timedelta(weeks=2):
                logger.info(f"Deleting expired '{queue_name}' failed job from {failed_job.enqueued_at}")
                failed_job.delete() # .cancel() doesn't delete the Redis hash
            else:
                len_failed_queue += 1

    if len_failed_queue:
        logger.info(f"Have {len_failed_queue} of our jobs in failed queue")
    return len_failed_queue
# end of handle_failed_queue function


# If a job has the same repo.full_name and ref that is already scheduled or queued, we cancel it so this one takes precedence
def cancel_similar_jobs(payload):
    if not payload or 'repository' not in payload or 'full_name' not in payload['repository'] or 'ref' not in payload:
        return
    logger.info("Checking if similar jobs already exist further up the queue to cancel them...")
    logger.info(payload)
    for queue in Queue.all(connection=redis_connection):
        if not queue or "handler" not in queue.name or "callback" in queue.name:
            continue
        job_ids = queue.scheduled_job_registry.get_job_ids() + queue.get_job_ids() + queue.started_job_registry.get_job_ids()
        for job_id in job_ids:
            job = queue.fetch_job(job_id)
            if job and len(job.args) > 0:
                pl = job.args[0]
                if pl and 'repository' in pl and 'full_name' in pl['repository'] and 'ref' in pl \
                    and payload['repository']['full_name'] == pl['repository']['full_name'] and payload['ref'] == pl['ref']:
                        logger.info(f"Found older job for repo: {pl['repository']['full_name']}, ref: {pl['ref']}")
                        try:
                            job.cancel()
                            stats_client.incr(f'{enqueue_job_stats_prefix}.canceled', 1)
                            logger.info(f"CANCELLED JOB {job.id} ({job.get_status()}) IN QUEUE {queue.name} DUE TO BEING SIMILAR TO NEW JOB")
                        except:
                            pass
# end of cancel_similar_jobs function

# This is the main workhorse part of this code
#   rq automatically returns a "Method Not Allowed" error for a GET, etc.
@app.route('/'+WEBHOOK_URL_SEGMENT, methods=['POST'])
def job_receiver():
    """
    Accepts POST requests and checks the (json) payload

    Queues the approved jobs at redis instance at global redis_hostname:6379.
    Queue name is djh_adjusted_webhook_queue_name and dcjh_adjusted_queue_name(may have been prefixed).
    """
    #assert request.method == 'POST'
    stats_client.incr(f'{enqueue_job_stats_prefix}.posts.attempted')
    logger.info(f"WEBHOOK received by {PREFIXED_LOGGING_NAME}: {request}")
    # NOTE: 'request' above typically displays something like "<Request 'http://git.door43.org/' [POST]>"

    djh_queue = Queue(djh_adjusted_webhook_queue_name, connection=redis_connection)
    # dcjh_queue = Queue(dcjh_adjusted_queue_name, connection=redis_connection)

    # Collect and log some helpful information
    len_djh_queue = len(djh_queue) # Should normally sit at zero here
    stats_client.gauge(f'{enqueue_job_stats_prefix}.queue.length.current', len_djh_queue)
    len_djh_failed_queue = handle_failed_queue(djh_adjusted_webhook_queue_name)
    stats_client.gauge(f'{enqueue_job_stats_prefix}.queue.length.failed', len_djh_failed_queue)
    # len_dcjh_queue = len(dcjh_queue) # Should normally sit at zero here
    # stats_client.gauge(f'{enqueue_catalog_job_stats_prefix}.queue.length.current', len_dcjh_queue)
    # len_dcjh_failed_queue = handle_failed_queue(dcjh_adjusted_queue_name)
    # stats_client.gauge(f'{enqueue_catalog_job_stats_prefix}.queue.length.failed', len_dcjh_failed_queue)

    # Find out how many workers we have
    total_worker_count = Worker.count(connection=redis_connection)
    logger.debug(f"Total rq workers = {total_worker_count}")
    djh_queue_worker_count = Worker.count(queue=djh_queue)
    logger.debug(f"Our {djh_adjusted_webhook_queue_name} queue workers = {djh_queue_worker_count}")
    stats_client.gauge(f'{enqueue_job_stats_prefix}.workers.available', djh_queue_worker_count)
    if djh_queue_worker_count < 1:
        logger.critical(f"{PREFIXED_DOOR43_JOB_HANDLER_QUEUE_NAME} has no job handler workers running!")
        # Go ahead and queue the job anyway for when a worker is restarted

    # dcjh_queue_worker_count = Worker.count(queue=dcjh_queue)
    # logger.debug(f"Our {dcjh_adjusted_queue_name} queue workers = {dcjh_queue_worker_count}")
    # stats_client.gauge(f'{enqueue_catalog_job_stats_prefix}.workers.available', dcjh_queue_worker_count)
    # if dcjh_queue_worker_count < 1:
    #     logger.critical(f"{PREFIXED_DOOR43_CATALOG_JOB_HANDLER_QUEUE_NAME} has no job handler workers running!")
    #     # Go ahead and queue the job anyway for when a worker is restarted

    response_ok_flag, response_dict = check_posted_payload(request, logger)
    # response_dict is json payload if successful, else error info
    if response_ok_flag:
        logger.debug(f"{PREFIXED_LOGGING_NAME} queuing good payload…")

        # Check for special switch to echo production requests to dev- chain
        global echo_prodn_to_dev_flag
        if not PREFIX: # Only apply to production chain
            try:
                repo_name = response_dict['repository']['full_name']
            except (KeyError, AttributeError):
                repo_name = None
            if repo_name == 'tx-manager-test-data/echo_prodn_to_dev_on':
                echo_prodn_to_dev_flag = True
                logger.info("TURNED ON 'echo_prodn_to_dev_flag'!\n")
                stats_client.incr(f'{enqueue_job_stats_prefix}.posts.succeeded')
                return jsonify({'success': True, 'status': 'echo ON'})
            if repo_name == 'tx-manager-test-data/echo_prodn_to_dev_off':
                echo_prodn_to_dev_flag = False
                logger.info("Turned off 'echo_prodn_to_dev_flag'.\n")
                stats_client.incr(f'{enqueue_job_stats_prefix}.posts.succeeded')
                return jsonify({'success': True, 'status': 'echo off'})

        # Add our fields
        response_dict['door43_webhook_retry_count'] = 0 # In case we want to retry failed jobs
        response_dict['door43_webhook_received_at'] = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ') # Used to calculate total elapsed time

        cancel_similar_jobs(response_dict)

        # NOTE: No ttl specified on the next line -- this seems to cause unrun jobs to be just silently dropped
        #           (For now at least, we prefer them to just stay in the queue if they're not getting processed.)
        #       The timeout value determines the max run time of the worker once the job is accessed
        scheduled = False
        if 'ref' in response_dict and "refs/tags" not in response_dict['ref'] and "master" not in response_dict['ref']:
            djh_queue.enqueue_in(timedelta(minutes=MINUTES_TO_WAIT), 'webhook.job', response_dict, job_timeout=WEBHOOK_TIMEOUT, result_ttl=(60*60*24), ttl=(60*60*24)) # A function named webhook.job will be called by the worker
            stats_client.incr(f'{enqueue_job_stats_prefix}.scheduled', 1)
            scheduled = True
        else:
            djh_queue.enqueue('webhook.job', response_dict, job_timeout=WEBHOOK_TIMEOUT, result_ttl=(60*60*24), ttl=(60*60*24)) # A function named webhook.job will be called by the worker        
            stats_client.incr(f'{enqueue_job_stats_prefix}.directly_queued', 1)
        # dcjh_queue.enqueue('webhook.job', response_dict, job_timeout=WEBHOOK_TIMEOUT) # A function named webhook.job will be called by the worker
        # NOTE: The above line can return a result from the webhook.job function. (By default, the result remains available for 500s.)

        len_djh_queue = len(djh_queue) # Update
        len_djh_scheduled = len(djh_queue.scheduled_job_registry.get_job_ids())
        if scheduled:
            # len_dcjh_queue = len(dcjh_queue) # Update
            logger.info(f"{PREFIXED_DOOR43_JOB_HANDLER_QUEUE_NAME} scheduled valid job to be added to the {djh_adjusted_webhook_queue_name} queue in {MINUTES_TO_WAIT} minutes [@{datetime.utcnow() + timedelta(minutes=MINUTES_TO_WAIT)}]" \
                        f" ({len_djh_scheduled} jobs scheduled, {len_djh_queue} jobs in queued " \
                            f"for {Worker.count(queue=djh_queue)} workers)" \
                        # f"({len_dcjh_queue} jobs now " \
                        #     f"for {Worker.count(queue=dcjh_queue)} workers, " \
                        # f"{len_djh_failed_queue} failed jobs) at {datetime.utcnow()}, " \
                        # f"{len_dcjh_failed_queue} failed jobs) at {datetime.utcnow()}\n"
            )
        else:
            logger.info(f"{PREFIXED_DOOR43_JOB_HANDLER_QUEUE_NAME} added to the {djh_adjusted_webhook_queue_name} queue  at {datetime.utcnow()}" \
                        f" ({len_djh_scheduled} jobs scheduled, {len_djh_queue} jobs in queued " \
                            f"for {Worker.count(queue=djh_queue)} workers)" \
                        # f"({len_dcjh_queue} jobs now " \
                        #     f"for {Worker.count(queue=dcjh_queue)} workers, " \
                        # f"{len_djh_failed_queue} failed jobs) at {datetime.utcnow()}, " \
                        # f"{len_dcjh_failed_queue} failed jobs) at {datetime.utcnow()}\n"
            )
        webhook_return_dict = {'success': True,
                               'status': 'queued',
                               'queue_name': djh_adjusted_webhook_queue_name,
                               'door43_job_queued_at': datetime.utcnow()}
        stats_client.incr(f'{enqueue_job_stats_prefix}.posts.succeeded')
        return jsonify(webhook_return_dict)
    #else:
    stats_client.incr(f'{enqueue_job_stats_prefix}.posts.invalid')
    response_dict['status'] = 'invalid'
    try:
        detail = request.headers['X-Gitea-Event']
    except KeyError:
        detail = "No X-Gitea-Event"
    logger.error(f"{PREFIXED_LOGGING_NAME} ignored invalid '{detail}' payload; responding with {response_dict}\n")
    return jsonify(response_dict), 400
# end of job_receiver()


@app.route('/'+CALLBACK_URL_SEGMENT, methods=['POST'])
def callback_receiver():
    """
    Accepts POST requests and checks the (json) payload

    Queues the approved jobs at redis instance at global redis_hostname:6379.
    Queue name is djh_adjusted_callback_queue_name (may have been prefixed).
    """
    #assert request.method == 'POST'
    stats_client.incr(f'{enqueue_callback_job_stats_prefix}.posts.attempted')
    logger.info(f"CALLBACK received by {PREFIXED_DOOR43_JOB_HANDLER_QUEUE_NAME}: {request}")

    # Collect (and log) some helpful information
    djh_queue = Queue(djh_adjusted_callback_queue_name, connection=redis_connection)
    len_djh_queue = len(djh_queue) # Should normally sit at zero here
    stats_client.gauge(f'{enqueue_callback_job_stats_prefix}.queue.length.current', len_djh_queue)
    len_djh_failed_queue = handle_failed_queue(djh_adjusted_callback_queue_name)
    stats_client.gauge(f'{enqueue_callback_job_stats_prefix}.queue.length.failed', len_djh_failed_queue)
    djh_queue_worker_count = Worker.count(queue=djh_queue)
    logger.debug(f"Our {djh_adjusted_callback_queue_name} queue workers = {djh_queue_worker_count}")
    stats_client.gauge(f'{enqueue_callback_job_stats_prefix}.workers.available', djh_queue_worker_count)

    response_ok_flag, response_dict = check_posted_callback_payload(request, logger)
    # response_dict is json payload if successful, else error info
    if response_ok_flag:
        logger.debug(f"{PREFIXED_LOGGING_NAME} queuing good callback…")

        # Add our fields
        response_dict['door43_callback_retry_count'] = 0

        # NOTE: No ttl specified on the next line -- this seems to cause unrun jobs to be just silently dropped
        #           (For now at least, we prefer them to just stay in the queue if they're not getting processed.)
        #       The timeout value determines the max run time of the worker once the job is accessed
        djh_queue.enqueue('callback.job', response_dict, job_timeout=CALLBACK_TIMEOUT, job_id=response_dict['job_id']) # A function named callback.job will be called by the worker
        # NOTE: The above line can return a result from the callback.job function. (By default, the result remains available for 500s.)

        # Find out who our workers are
        #workers = Worker.all(connection=redis_connection) # Returns the actual worker objects
        #logger.debug(f"Total rq workers ({len(workers)}): {workers}")
        #djh_queue_workers = Worker.all(queue=djh_queue)
        #logger.debug(f"Our {djh_adjusted_callback_queue_name} queue workers ({len(djh_queue_workers)}): {djh_queue_workers}")

        # Find out how many workers we have
        #worker_count = Worker.count(connection=redis_connection)
        #logger.debug(f"Total rq workers = {worker_count}")
        #djh_queue_worker_count = Worker.count(queue=djh_queue)
        #logger.debug(f"Our {djh_adjusted_callback_queue_name} queue workers = {djh_queue_worker_count}")

        len_djh_queue = len(djh_queue) # Update
        logger.info(f"{PREFIXED_DOOR43_JOB_HANDLER_QUEUE_NAME} queued valid callback job to {djh_adjusted_callback_queue_name} queue " \
                    f"({len_djh_queue} jobs now " \
                        f"for {Worker.count(queue=djh_queue)} workers, " \
                    f"{len_djh_failed_queue} failed jobs) at {datetime.utcnow()}\n")

        callback_return_dict = {'success': True,
                                'status': 'queued',
                                'queue_name': djh_adjusted_callback_queue_name,
                                'door43_callback_queued_at': datetime.utcnow()}
        stats_client.incr(f'{enqueue_callback_job_stats_prefix}.posts.succeeded')
        return jsonify(callback_return_dict)
    #else:
    stats_client.incr(f'{enqueue_callback_job_stats_prefix}.posts.invalid')
    response_dict['status'] = 'invalid'
    logger.error(f"{PREFIXED_LOGGING_NAME} ignored invalid callback payload; responding with {response_dict}\n")
    return jsonify(response_dict), 400
# end of callback_receiver()

@app.route('/'+WEBHOOK_URL_SEGMENT+"status/", methods=['GET'])
def status():
    queue_names = [PREFIXED_DOOR43_JOB_HANDLER_QUEUE_NAME, PREFIX + "tx_job_handler", PREFIX + "tx_job_handler_priority", PREFIX + "tx_job_handler_pdf", PREFIXED_DOOR43_JOB_HANDLER_CALLBACK_QUEUE_NAME]
    status_order = ["scheduled", "enqueued", "started", "finished", "failed", 'canceled']
    rows = {}
    for q_name in queue_names:
        queue = Queue(q_name, connection=redis_connection)
        rows[q_name] = {}
        for status in status_order:
            rows[q_name][status] = {}
        for id in queue.scheduled_job_registry.get_job_ids():
            job = queue.fetch_job(id)
            if job:
                rows[q_name]["scheduled"][job.created_at.strftime(f'%Y-%m-%d %H:%M:%S {job.id}')] = get_job_list_html(job)
        for job in queue.get_jobs():
            rows[q_name]["enqueued"][job.created_at.strftime(f'%Y-%m-%d %H:%M:%S {job.id}')] = get_job_list_html(job)
        for id in queue.started_job_registry.get_job_ids():
            job = queue.fetch_job(id)
            if job:
                rows[q_name]["started"][job.created_at.strftime(f'%Y-%m-%d %H:%M:%S {job.id}')] = get_job_list_html(job)
        for id in queue.finished_job_registry.get_job_ids():
            job = queue.fetch_job(id)
            if job:
                rows[q_name]["finished"][job.created_at.strftime(f'%Y-%m-%d %H:%M:%S {job.id}')] = get_job_list_html(job)
        for id in queue.failed_job_registry.get_job_ids():
            job = queue.fetch_job(id)
            if job:
                rows[q_name]["failed"][job.created_at.strftime(f'%Y-%m-%d %H:%M:%S {job.id}')] = get_job_list_html(job)
        for id in queue.canceled_job_registry.get_job_ids():
            job = queue.fetch_job(id)
            if job:
                rows[q_name]["canceled"][job.created_at.strftime(f'%Y-%m-%d %H:%M:%S {job.id}')] = get_job_list_html(job)
    html = "<table cellpadding=10 colspacing=10 border=2><tr>"
    for q_name in queue_names:
        html += f"<th>{q_name} Queue</th>"
    html += "</tr>"
    for status in status_order:
        html += "<tr>"
        for q_name in queue_names:
            html += f"<td><h3>{status.capitalize()} Registery</h3>"
            keys = rows[q_name][status].keys()
            sorted(keys)
            for key in keys:
                html += rows[q_name][status][key]
            html += "</td>"
        html += "</tr>"
    html += "</table><br/><br/>"
    html += f'''<div>
<form method="POST" action="../" style="display:block;clear:both">
    <textarea name="payload" rows=5 cols="50"></textarea>
    <br/><br/>
    <input type="submit" value="Queue Job"/>
</form></div>'''
    return html


@app.route('/'+WEBHOOK_URL_SEGMENT+"status/job/<job_id>", methods=['GET'])
def getJob(job_id):
    queues = Queue.all(connection=redis_connection)
    for queue in queues:
        job = queue.fetch_job(job_id)
        if job:
            break
    if not job or not job.args:
        return f"<h1>JOB {job_id} NOT FOUND</h1>"
    repo = get_repo_from_job(job)
    type = get_ref_type_from_job(job)
    ref = get_ref_from_job(job)
    html = f'<h1>JOB ID: {job_id}</h1>'
    html += f'<h2><b>Repo:</b> <a href="https://git.door43.org/{repo}/src/{type}/{ref}" target="_blank">{repo}</a></h2>'
    html += f'<h3>{get_ref_type_from_job(job)}: {get_ref_from_job(job)}</h3>'
    html += f'<p>Status: {job.get_status()}<br/>'
    if job.enqueued_at:
        html += f'Enqued at: {job.enqueued_at}{f" ({job.get_position()})" if job.is_queued else ""}<br/>'
    if job.started_at:
        html += f'Started: {job.started_at}<br/>'
    if job.ended_at:
        html += f'Ended: {job.ended_at} {round((job.ended_at-job.enqueued_at).total_seconds() / 60)}'
    if job.is_failed:
        html += f"<div><b>Latest Result</b><p>{job.latest_result()}</p></div>"
    html += f'<div><p><b>Payload:</b>'
    html += f'<form method="POST" action"../../">'
    html += f'<textarea cols=200 rows=20>'
    try:
        html += json.dumps(job.args[0], indent=2)
    except:
        pass
    html += f'</textarea>'
    html += f'<input type="submit" value="Queue again" />'
    html += f'</form></p></div>'
    html += f'<br/><br/><p><a href="../" style="text-decoration:none"><== Go back to queue lists</a></p><br/><br/>'
    return html


def get_job_list_html(job):
    html = f'<a href="job/{job.id}">{job.id[:5]}</a>: {get_dcs_link(job)}<br/>'
    times = []
    if job.created_at:
        times.append(f'created {job.created_at.strftime("%Y-%m-%d %H:%M:%S")}')
    if job.enqueued_at:
        times.append(f'enqued {job.enqueued_at.strftime("%Y-%m-%d %H:%M:%S")}')
    if job.started_at:
        times.append(f'started {job.started_at.strftime("%Y-%m-%d %H:%M:%S")}')
    if job.ended_at:
        times.append(f'ended {job.started_at.strftime("%Y-%m-%d %H:%M:%S")} ({round((job.ended_at-job.enqueued_at).total_seconds() / 60)})')
    if len(times) > 0:
        html += '<div style="font-style: italic; color: #929292">'
        html += ';<br/>'.join(times)
        html += '</div>'
    return html


def get_repo_from_job(job):
    if not job or not job.args:
        return None
    payload = job.args[0]
    if "repo_name" in payload and "repo_owner" in payload:
        return f'{payload["repo_owner"]}/{payload["repo_name"]}'
    elif "repository" in payload and "full_name" in payload["repository"]:
        return payload["repository"]["full_name"]

  
def get_ref_from_job(job):  
    if not job or not job.args:
        return None
    payload = job.args[0]
    if "repo_ref" in payload and "repo_ref_type" in payload:
        return payload["repo_ref"]
    elif "ref" in payload:
        ref_parts = payload["ref"].split("/")
        return ref_parts[-1]


def get_ref_type_from_job(job):
    if not job or not job.args:
        return None
    payload = job.args[0]
    if "repo_ref" in payload and "repo_ref_type" in payload:
        return payload["repo_ref_type"]
    elif "ref" in payload:
        ref_parts = payload["ref"].split("/")
        if ref_parts[1] == "tags":
            return "tag"
        else:
            return "branch"


def get_dcs_link(job):
    repo = get_repo_from_job(job)
    ref = get_ref_from_job(job)
    type = get_ref_type_from_job(job)
    if not repo or not ref:
        return 'INVALID'
    return f'<a href="https://git.door43.org/{repo}/src/{type}/{ref}" target="_blank">{repo} : {ref}</a>'


if __name__ == '__main__':
    app.run()
