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

        if response_dict['DCS_event'] == "push":
            cancel_similar_jobs(response_dict)

        # NOTE: No ttl specified on the next line -- this seems to cause unrun jobs to be just silently dropped
        #           (For now at least, we prefer them to just stay in the queue if they're not getting processed.)
        #       The timeout value determines the max run time of the worker once the job is accessed
        scheduled = False
        job_id = None
        if "job_id" in response_dict:
            job_id = response_dict["job_id"]
        if 'ref' in response_dict and "refs/tags" not in response_dict['ref'] and "master" not in response_dict['ref'] and response_dict["DCS_event"] == "push":
            job = djh_queue.enqueue_in(timedelta(minutes=MINUTES_TO_WAIT), 'webhook.job', response_dict, job_id=job_id, job_timeout=WEBHOOK_TIMEOUT, result_ttl=(60*60*3)) # A function named webhook.job will be called by the worker
            stats_client.incr(f'{enqueue_job_stats_prefix}.scheduled')
            scheduled = True
        else:
            job = djh_queue.enqueue('webhook.job', response_dict, job_id=job_id, job_timeout=WEBHOOK_TIMEOUT, result_ttl=(60*60*3)) # A function named webhook.job will be called by the worker
            stats_client.incr(f'{enqueue_job_stats_prefix}.directly_queued')
        # dcjh_queue.enqueue('webhook.job', response_dict, job_timeout=WEBHOOK_TIMEOUT) # A function named webhook.job will be called by the worker
        # NOTE: The above line can return a result from the webhook.job function. (By default, the result remains available for 500s.)

        len_djh_queue = len(djh_queue) # Update
        len_djh_scheduled = len(djh_queue.scheduled_job_registry.get_job_ids())
        if scheduled:
            # len_dcjh_queue = len(dcjh_queue) # Update
            logger.info(f"{PREFIXED_DOOR43_JOB_HANDLER_QUEUE_NAME} scheduled valid job with ID {job.id} to be added to the {djh_adjusted_webhook_queue_name} queue in {MINUTES_TO_WAIT} minutes [@{datetime.utcnow() + timedelta(minutes=MINUTES_TO_WAIT)}]" \
                        f" ({len_djh_scheduled} jobs scheduled, {len_djh_queue} jobs in queued " \
                            f"for {Worker.count(queue=djh_queue)} workers)" \
                        # f"({len_dcjh_queue} jobs now " \
                        #     f"for {Worker.count(queue=dcjh_queue)} workers, " \
                        # f"{len_djh_failed_queue} failed jobs) at {datetime.utcnow()}, " \
                        # f"{len_dcjh_failed_queue} failed jobs) at {datetime.utcnow()}\n"
            )
        else:
            logger.info(f"{PREFIXED_DOOR43_JOB_HANDLER_QUEUE_NAME} added valid job with ID {job.id} to the {djh_adjusted_webhook_queue_name} queue  at {datetime.utcnow()}" \
                        f" ({len_djh_scheduled} jobs scheduled, {len_djh_queue} jobs in queued " \
                            f"for {Worker.count(queue=djh_queue)} workers)" \
                        # f"({len_dcjh_queue} jobs now " \
                        #     f"for {Worker.count(queue=dcjh_queue)} workers, " \
                        # f"{len_djh_failed_queue} failed jobs) at {datetime.utcnow()}, " \
                        # f"{len_dcjh_failed_queue} failed jobs) at {datetime.utcnow()}\n"
            )
        webhook_return_dict = {'success': True,
                               'status': 'scheduled' if scheduled else 'queued',
                               'job_id': job.id,
                               'queue_name': djh_adjusted_webhook_queue_name,
                               'door43_job_queued_at': datetime.utcnow(),
                               "job_status_url": f"https://git.door43.org/client/webhook/status/job/{job.id}"}
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
        djh_queue.enqueue('callback.job', response_dict, job_timeout=CALLBACK_TIMEOUT, job_id="callback_"+response_dict['job_id'], result_ttl=(60*60*24)) # A function named callback.job will be called by the worker
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


queue_names = ["door43_job_handler", "tx_job_handler", "tx_job_handler_priority", "tx_job_handler_pdf", "door43_job_handler_callback"]
queue_desc = {
    DOOR43_JOB_HANDLER_QUEUE_NAME: "Lints files & massages files for tX, uploads to cloud, sends work request to tx_job_handler",
    "tx_job_handler": "Handles branches that are not master (user branches), converts to HTML, uploads result to cloud, sends a work request to door43 callback",
    "tx_job_handler_priority": "Handles master branch and tags (releass), converting to HTML, uploads result to cloud, sends a work request to door43 callback",
    "tx_job_handler_pdf": "Handles PDF requests, converting to PDF, uploads result to cloud, sends a work request door43 callback",
    DOOR43_JOB_HANDLER_CALLBACK_QUEUE_NAME: "Fetches coverted files from cloud, deploys to Door43 Preview (door43.org) for HTML and PDF jobs",
}
registry_names = ["scheduled", "enqueued", "started", "finished", "failed", 'canceled']

@app.route('/' + WEBHOOK_URL_SEGMENT + 'sbmit/', methods = ['GET'])
def getSubmitForm():
    f = open('./payload.json')
    payload = f.read()
    f.close()

    html += f'''
<form>
    <b>Payload:</b><br/><textarea id="payload" rows="5" cols="50">{payload}</textarea>
    <br/>
    <b>DCS_event:</b> <input type="text" id="DCS_event" value="push" />
    <br/>
    <input type="button" value="Queue Job" onClick="submitForm()"/>
</form>
'''
    html += '''<script type="text/javascript">
    function submitForm() {
        var payload = document.getElementById("payload");
        var dcs_event = document.getElementById("DCS_event");
        var xhr = new XMLHttpRequest();
        xhr.open("POST", "/", true);
        xhr.setRequestHeader('Content-Type', 'application/json; charset=UTF-8');
        xhr.setRequestHeader('X-Gitea-Event', dcs_event.value);
        xhr.setRequestHeader('X-Gitea-Event-Type', dcs_event.value)
        xhr.onreadystatechange = () => {
            if (xhr.readyState === 4) {
                alert(xhr.response);
                console.log(xhr.response);
            }
        };
        console.log(payload.value);
        console.log(event.value);
        xhr.send(payload.value);
    }
</script>
'''
    return html


@app.route('/' + WEBHOOK_URL_SEGMENT + "status/", methods = ['GET'])
def getStatusTable():
    html = ""
    job_id_filter = request.args.get("job_id")
    repo_filter = request.args.get("repo")
    ref_filter = request.args.get("ref")
    event_filter = request.args.get("event")
    show_canceled = request.args.get("show_canceled",  default=False, type=bool)

    if len(request.args) > (1 if show_canceled else 0):
        html += f'<p>Table is filtered. <a href="?{"show_canceled=true" if show_canceled else ""}">Click to Show All Jobs</a></p>'

    html += f'<form METHOD="GET">'
    if repo_filter:
        html += f'<input type="hidden" name="repo" value="{repo_filter}"/>'
    if ref_filter:
        html += f'<input type="hidden" name="ref" value="{ref_filter}"/>'
    if event_filter:
        html += f'<input type="hidden" name="event" value="{event_filter}"/>'
    html += f'<input type="checkbox" name="show_canceled" value="true" onChange="this.form.submit()"  {"checked" if show_canceled else ""}/> Show canceled</form>'

    job_map = get_job_map(job_id_filter=job_id_filter, repo_filter=repo_filter, ref_filter=ref_filter, event_filter=event_filter, show_canceled=show_canceled)

    html += '<table cellpadding="10" colspacing="10" border="2"><tr>'
    for i, q_name in enumerate(queue_names):
        html += f'<th style="vertical-align:top">{q_name}{"&rArr;tx" if i==0 else "&rArr;callback" if i<(len(queue_names)-1) else ""}</th>'
    html += '</tr><tr>'
    for q_name in queue_names:
        html += f'<td style="font-style: italic;font-size:0.8em;vertical-align:top">{queue_desc[q_name]}</td>'
    html += '</tr>'
    for r_name in registry_names:
        if r_name == "canceled" and not show_canceled:
            continue
        html += '<tr>'
        for q_name in queue_names:
            html += f'<td style="vertical-align:top"><h3>{r_name.capitalize()} Registery</h3>'
            sorted_ids = sorted(job_map[q_name][r_name].keys(), key=lambda id: job_map[q_name][r_name][id]["job"].created_at, reverse=True)
            for id in sorted_ids:
                html += get_job_list_html(job_map[q_name][r_name][id]["job"])
            html += '</td>'
        html += '</tr>'
    html += '</table><br/><br/>'
    return html

def get_job_map(job_id_filter=None, repo_filter=None, ref_filter=None, event_filter=None, show_canceled=False):
    job_map = {}

    def add_to_job_map(queue_name, registry_name, job):
        if not job or not job.args:
            return
        repo = get_repo_from_payload(job.args[0])
        ref = get_ref_from_payload(job.args[0])
        event = get_event_from_payload(job.args[0])
        job_id = job.id.split('_')[-1]

        if (job_id_filter and job_id_filter != job_id) \
            or (repo_filter and repo_filter != repo) \
            or (ref_filter and ref_filter != ref) \
            or (event_filter and event_filter != event):
            return

        canceled = job.args[0]["canceled"] if "canceled" in job.args[0] else []
        if job_id not in job_map[queue_name][registry_name]:
            job_map[queue_name][registry_name][job_id] = {}
        job_map[queue_name][registry_name][job_id]["job"] = job
        job_map[queue_name][registry_name][job_id]["canceled"] = canceled

        for qn in job_map:
            for rn in job_map[qn]:
                for id in job_map[qn][rn]:
                    j = job_map[qn][rn][id]["job"]
                    c = job_map[qn][rn][id]["canceled"] if "canceled" in job_map[qn][rn][id]["job"].args[0] else []
                    if id != job_id and (job.is_canceled or j.is_canceled):
                        if id in canceled:
                            job_map[qn][rn][id]["canceled_by"] = job_id
                        elif job_id in c:
                            job_map[queue_name][registry_name][job_id]["canceled_by"] = id

    for queue_name in queue_names:
        queue = Queue(PREFIX + queue_name, connection=redis_connection)
        job_map[queue_name] = {}
        for registry_name in registry_names:
            job_map[queue_name][registry_name] = {}
        for id in queue.scheduled_job_registry.get_job_ids():
            if not job_id_filter or job_id_filter == id.split('_')[-1]:
                add_to_job_map(queue_name, "scheduled", queue.fetch_job(id))
        for job in queue.get_jobs():
            if not job_id_filter or job_id_filter == id.split('_')[-1]:
                add_to_job_map(queue_name, "enqueued", job)
        for id in queue.started_job_registry.get_job_ids():
            if not job_id_filter or job_id_filter == id.split('_')[-1]:
               add_to_job_map(queue_name, "started", queue.fetch_job(id))
        for id in queue.finished_job_registry.get_job_ids():
            if not job_id_filter or job_id_filter == id.split('_')[-1]:
                add_to_job_map(queue_name, "finished", queue.fetch_job(id))
        for id in queue.failed_job_registry.get_job_ids():
            if not job_id_filter or job_id_filter == id.split('_')[-1]:
                add_to_job_map(queue_name, "failed", queue.fetch_job(id))
        if show_canceled:
            if not job_id_filter or job_id_filter == id:
                for id in queue.canceled_job_registry.get_job_ids():
                    add_to_job_map(queue_name, "canceled", queue.fetch_job(id))
    return job_map


@app.route('/'+WEBHOOK_URL_SEGMENT+"status/job/<job_id>", methods=['GET'])
def getJob(job_id):
    html = f'<p><a href="../" style="text-decoration:none">&larr; Go back</a></p>'
    html += f'<p><a href="../?job_id={job_id}" style="text-decoration:none">&larr; See only this job in queues</a></p>'

    job = None
    for q_name in queue_names:
        logger.error(q_name)
        prefix = f'{q_name}_' if q_name != DOOR43_JOB_HANDLER_QUEUE_NAME else ""
        queue = Queue(PREFIX+q_name, connection=redis_connection)
        job = queue.fetch_job(prefix+job_id)
        if job:
            break
    if not job or not job.args:
        return f"<h1>JOB NOT FOUND: {job_id}</h1>"

    job_map = get_job_map(repo_filter=get_repo_from_payload(job.args[0]), ref_filter=get_ref_from_payload(job.args[0]))

    jobs_html = ""
    job_infos = []
    last_queue = None
    last_job_info = None

    for queue_name in job_map:
        for registry_name in job_map[queue_name]:
            if job_id in job_map[queue_name][registry_name]:
                job_info = job_map[queue_name][registry_name][job_id]
                job_infos.append(job_info)
                last_queue = queue_name
                last_job_info = job_info
                jobs_html += get_queue_job_info_html(queue_name, registry_name, job_info)

    if len(job_infos) < 1:
        return html+"<h1>NO JOB FOUND!</h1>"
    
    job = job_infos[0]["job"]
    last_job = last_job_info["job"]
    repo = get_repo_from_payload(job.args[0])
    ref_type = get_ref_type_from_payload(job.args[0])
    ref = get_ref_from_payload(job.args[0])
    event = get_event_from_payload(job.args[0])

    html += f'<h1>JOB ID: {job_id}</h1>'
    html += "<p>"
    html += f'<b>Repo:</b> <a href="https://git.door43.org/{repo}/src/{ref_type}/{ref}" target="_blank">{repo}</a><br/>'
    html += f'<b>{ref_type.capitalize()}:</b> {ref}<br/>'
    html += f'<b>Event:</b> {event}'
    html += f'</p>'
    html += "<p>"
    html += "<h2>Overall Stats</h2>"
    html += f'<b>Status:</b> {last_job.get_status()}<br/>'
    html += f'<b>Final Queue:</b> {last_queue}<br/><br/>'
    html += f'<b>Created at:</b>{job.created_at.strftime("%Y-%m-%d %H:%M:%S")}<br/>'
    if job.started_at:
        html += f'<b>Started at:</b> {job.started_at.strftime("%Y-%m-%d %H:%M:%S")}<br/>'
    html += f'{get_job_final_status_and_time(job.created_at, last_job)}<br/>'
    if "canceled_by" in last_job_info:
        html += f'<b>Canceled by a similar job:</b> <a href="{last_job_info["canceled_by"]}">{last_job_info["canceled_by"]}</a><br/>'
    if "canceled" in job_info and len(job_info["canceled"]) > 0:
        jobs_canceled = []
        for id in job_info["canceled"]:
            jobs_canceled.append(f'<a href="{id}">{id}</a>')
        html += f'<b>This job canceled previous jobs(s):</b> {", ".join(jobs_canceled)}<br/>'
    html += "</p>"
    if last_job.is_failed or last_job.exc_info:
        html += f'<div><b>ERROR:</b><p><pre>{last_job.exc_info}</pre></p></div>'
    elif last_job.result:
        html += f'<div><b>Result:</b><p>{last_job.result}</p></div>'

    html += jobs_html

    return html


@app.route('/'+WEBHOOK_URL_SEGMENT+"status/clear/failed", methods=['GET'])
def clearFailed():
    for queue_name in queue_names:
        queue = Queue(queue_name, connection=redis_connection)
        for job_id in queue.failed_job_registry.get_job_ids():
            job = queue.fetch_job(job_id)
            job.delete()
    return "Failed jobs cleared"


@app.route('/'+WEBHOOK_URL_SEGMENT+"status/clear/canceled", methods=['GET'])
def clearCanceled():
    for queue_name in queue_names:
        queue = Queue(queue_name, connection=redis_connection)
        for job_id in queue.canceled_job_registry.get_job_ids():
            job = queue.fetch_job(job_id)
            job.delete()
    return "Canceled jobs cleared"


def get_queue_job_info_html(queue_name, registry_name, job_info):
    if not job_info:
        return ""
    job = job_info["job"]

    html = f"<h2>Queue: {PREFIX+queue_name}</h2>"
    html += f'<div><b>Status:</b> {job.get_status()}<br/>'
    if job.created_at:
        html += f'<b>Created at:</b> {job.created_at.strftime("%Y-%m-%d %H:%M:%S")}<br/>'
    if job.enqueued_at:
        html += f'<b>Enqued at:</b> {job.enqueued_at.strftime("%Y-%m-%d %H:%M:%S")}{f" (Position: {job.get_position()+1})" if job.is_queued else ""}<br/>'
    if job.started_at:
        html += f'<b>Started:</b> {job.started_at.strftime("%Y-%m-%d %H:%M:%S")}<br/>'
    if job.ended_at:
        if job.get_status() == "failed":
            html += f'<b>Failed:</b> '
        else:
            html += f'<b>Ended:</b> '
        html += f'{job.ended_at.strftime("%Y-%m-%d %H:%M:%S")} ({get_relative_time(job.started_at, job.ended_at)})<br/>'
    html += "</div>"
    if job.result:
        html += f'<div style="padding-top: 10px"><b>Result:</b><br/>{job.result}</div>'
    if job.args and job.args[0]:
        html += f'<div><p><b>Payload:</b>'
        html += f'<form>'
        html += f'<textarea id="payload" cols="200" rows="10" id="payload">'
        try:
            html += json.dumps(job.args[0], indent=2)
        except:
            html += f"{job.args[0]}"
        html += f'</textarea>'
    if queue_name == DOOR43_JOB_HANDLER_QUEUE_NAME:
        html += '''<br/><br/>
    <input type="button" value="Re-Queue Job" onClick="submitForm()"/>
<script type="text/javascript">
    function submitForm() {
        var payload = document.getElementById("payload");
        var payloadJSON = JSON.parse(payload.value);
        console.log(payloadJSON);
        console.log(payloadJSON["DCS_event"]);
        delete payloadJSON["canceled"];
        var xhr = new XMLHttpRequest();
        xhr.open("POST", "../../", true);
        xhr.setRequestHeader('Content-Type', 'application/json; charset=UTF-8');
        xhr.setRequestHeader('X-Gitea-Event', payloadJSON["DCS_event"]);
        xhr.setRequestHeader('X-Gitea-Event-Type', payloadJSON["DCS_event"]);
        xhr.onreadystatechange = () => {
            if (xhr.readyState === 4) {
                alert(xhr.response);
                console.log(xhr.response);
            }
        };
        xhr.send(JSON.stringify(payloadJSON));
    }
</script>
'''
    html += f'</form></p></div>'
    return html


def get_job_final_status_and_time(created_time, job):
    if job.is_scheduled:
        return f'<b>Scheduled at:</b> {job.created_at.strftime("%Y-%m-%d %H:%M:%S")}<br/>'+get_elapsed_time(created_time, job.created_at)
    if job.is_queued:
        return f'<b>Enqeued at:</b> {job.enqueued_at.strftime("%Y-%m-%d %H:%M:%S")}<br/>'+get_elapsed_time(created_time, job.enqueued_at)
    if job.ended_at:
        if job.get_status() == "failed":
            html = f'<b>Failed at:</b> '
        else:
            html = f'<b>Ended at:</b> '
        return f'{html}{job.ended_at.strftime("%Y-%m-%d %H:%M:%S")}<br/>'+get_elapsed_time(created_time, job.ended_at)
    if job.is_canceled:
        end_time = job.ended_at if job.ended_at else job.started_at if job.started_at else job.enqueued_at if job.enqueued_at else job.created_at
        return f'<b>Canceled at:</b> {job.created_at.strftime("%Y-%m-%d %H:%M:%S")}<br/>'+get_elapsed_time(created_time, end_time)
    return ""


def get_elapsed_time(start, end):
    if not start or not end or start >= end:
        return ""
    return "<b>Elapsed Time:</b> "+get_relative_time(start, end)


def get_relative_time(start=None, end=None):
    if not end:
        end = datetime.utcnow()
    if not start:
        start = end
    ago = round((end - start).total_seconds())
    t = "s"
    if ago > 120:
        ago = round(ago / 60)
        t = "m"
        if ago > 120:
            ago = round(ago / 60)
            t = "h"
            if ago > 48:
                ago = round(ago / 24)
                t = "d"
    return f"{ago}{t}"


def get_job_list_html(job):
    job_id = job.id.split('_')[-1]
    html = f'<a href="job/{job_id}">{job_id[:5]}</a>: {get_job_list_filter_link(job)}<br/>'
    if job.ended_at:
        timeago = f'{get_relative_time(job.ended_at)} ago'
        runtime = get_relative_time(job.started_at, job.ended_at)
        end_word = "canceled" if job.is_canceled else "failed" if job.is_failed else "finished"
        html += f'<div style="padding-left:5px;font-style:italic;" title="started: {job.started_at.strftime("%Y-%m-%d %H:%M:%S")}; {end_word}: {job.ended_at.strftime("%Y-%m-%d %H:%M:%S")}">ran for {runtime}, {end_word} {timeago}</div>'
    elif job.is_started:
        timeago = f'{get_relative_time(job.started_at)} ago'
        html += f'<div style="padding-left:5px;font-style:italic"  title="started: {job.started_at.strftime("%Y-%m-%d %H:%M:%S")}">started {timeago}</div>'
    elif job.is_queued:
        timeago = f'{get_relative_time(job.enqueued_at)}'
        html += f'<div style="padding-left:5px;font-style:italic;" title="queued: {job.enqueued_at.strftime("%Y-%m-%d %H:%M:%S")}">queued for {timeago}</div>'
    elif job.get_status(True) == "scheduled":
        timeago = f'{get_relative_time(job.created_at)}'
        html += f'<div style="padding-left:5px;font-style:italic;" title="scheduled: {job.created_at.strftime("%Y-%m-%d %H:%M:%S")}">schedued for {timeago}</div>'
    else:
        timeago = f'{get_relative_time(job.created_at)} ago'
        html += f'<div style="padding-left:5px;font-style:italic;" title="created: {job.created_at.strftime("%Y-%m-%d %H:%M:%S")}">created {timeago}, status: {job.get_status()}</div>'
    return html


def get_repo_from_payload(payload):
    if not payload:
        return None
    if "repo_name" in payload and "repo_owner" in payload:
        return f'{payload["repo_owner"]}/{payload["repo_name"]}'
    elif "repository" in payload and "full_name" in payload["repository"]:
        return payload["repository"]["full_name"]

  
def get_ref_from_payload(payload):
    if not payload:
        return None
    if "repo_ref" in payload and "repo_ref_type" in payload:
        return payload["repo_ref"]
    elif "ref" in payload:
        ref_parts = payload["ref"].split("/")
        return ref_parts[-1]
    elif "release" in payload and "tag_name" in payload["release"]:
        return payload["release"]["tag_name"]
    elif "DCS_event" in payload and payload["DCS_event"] == "fork":
        return "master"


def get_ref_type_from_payload(payload):
    if not payload:
        return None
    if "repo_ref" in payload and "repo_ref_type" in payload:
        return payload["repo_ref_type"]
    elif "ref" in payload:
        ref_parts = payload["ref"].split("/")
        if ref_parts[1] == "tags":
            return "tag"
        elif ref_parts[1] == "heads":
            return "branch"
    elif "DCS_event" in payload:
        if payload["DCS_event"] == "fork":
            return "branch"
        elif payload["DCS_event"] == "release":
            return "tag"


def get_event_from_payload(payload):
    if not payload:
        return None
    if 'DCS_event' in payload:
        return payload['DCS_event']
    else:
        return 'push'


def get_job_list_filter_link(job):
    repo = get_repo_from_payload(job.args[0])
    ref = get_ref_from_payload(job.args[0])
    event = get_event_from_payload(job.args[0])
    return f'<a href="?repo={repo}" title="{repo}">{repo.split("/")[-1]}&#128172;</a>=><a href="?repo={repo}&ref={ref}">{ref}</a>=><a href="?repo={repo}&ref={ref}&event={event}">{event}</a>'


def get_dcs_link(job):
    repo = get_repo_from_payload(job.args[0]) if job.args else None
    ref_type = get_ref_type_from_payload(job.args[0]) if job.args else 'branch'
    ref = get_ref_from_payload(job.args[0]) if job.args else 'master'
    event = get_event_from_payload(job.args[0]) if job.args else 'push'
    if not repo:
        return 'INVALID'
    text = f'{repo.split("/")[-1]}=>{event}=>{ref}'
    if event != "delete":
        return f'<a href="https://git.door43.org/{repo}/src/{ref_type}/{ref}" target="_blank" title="{repo}/src/{type}/{ref}">{text}</a>'
    else:
        return text

# If a job has the same repo.full_name and ref that is already scheduled or queued, we cancel it so this one takes precedence
def cancel_similar_jobs(incoming_payload):
    if not incoming_payload or 'repository' not in incoming_payload or 'full_name' not in incoming_payload['repository'] or 'ref' not in incoming_payload:
        return
    logger.info("Checking if similar jobs already exist further up the queue to cancel them...")
    logger.info(incoming_payload)
    my_repo = get_repo_from_payload(incoming_payload)
    my_ref = get_ref_from_payload(incoming_payload)
    my_event = get_event_from_payload(incoming_payload)
    if not my_repo or not my_ref or my_event != "push":
        return
    for queue_name in queue_names:
        # Don't want to cancel anything being called back - let it happen
        if queue_name == DOOR43_JOB_HANDLER_CALLBACK_QUEUE_NAME:
            continue
        queue = Queue(PREFIX + queue_name, connection=redis_connection)
        job_ids = queue.scheduled_job_registry.get_job_ids() + queue.get_job_ids() + queue.started_job_registry.get_job_ids()
        for job_id in job_ids:
            job = queue.fetch_job(job_id)
            if job and len(job.args) > 0:
                pl = job.args[0]
                old_repo = get_repo_from_payload(pl)
                old_ref = get_ref_from_payload(pl)
                old_event = get_event_from_payload(pl)
                if my_repo == old_repo and my_ref == old_ref and old_event == "push":
                        logger.info(f"Found older job for repo: {old_repo}, ref: {old_ref}")
                        try:
                            job.cancel()
                            # stats_client.incr(f'{enqueue_job_stats_prefix}.canceled')
                            logger.info(f"CANCELLED JOB {job.id} ({job.get_status()}) IN QUEUE {queue.name} DUE TO BEING SIMILAR TO NEW JOB")
                            if "canceled" not in incoming_payload:
                                incoming_payload["canceled"] = []
                            incoming_payload["canceled"].append(job.id)
                        except:
                            pass
# end of cancel_similar_jobs function


if __name__ == '__main__':
    app.run()
