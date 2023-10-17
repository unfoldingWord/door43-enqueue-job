# This code adapted by RJH June 2018 from tx-manager/client_webhook/ClientWebhookHandler
#   Updated Sept 2018 to add callback check

import os
from typing import Dict, Tuple, List, Any, Optional

prefix = os.getenv('QUEUE_PREFIX', '')
DCS_URL = os.getenv('DCS_URL', default='https://develop.door43.org' if prefix else 'https://git.door43.org')

RESTRICT_DCS_URL = os.getenv('RESTRICT_DCS_URL', 'True').lower() not in ['false', '0', 'f', '']
DCS_URL = os.getenv('DCS_URL', 'https://git.door43.org' if not prefix else 'https://develop.door43.org')
UNWANTED_REPO_OWNER_USERNAMES = (  # code repos, not "content", so don't convert—blacklisted
                                'translationCoreApps',
                                'unfoldingWord-box3',
                                'unfoldingWord-dev',
                                )


def check_posted_payload(request, logger) -> Tuple[bool, Dict[str,Any]]:
    """
    Accepts webhook notification from DCS.
        Parameter is a rq request object

    Returns a 2-tuple:
        True or False if payload checks out
        Either the payload that was checked (if returning True above),
            or the error dict (if returning False above)
    """
    # Bail if this is not a POST with a payload
    if not request.data:
        logger.error("Received request but no payload found")
        return False, {'error': 'No payload found. You must submit a POST request via a DCS webhook notification.'}

    # Check for a test ping from Nagios
    if 'User-Agent' in request.headers and 'nagios-plugins' in request.headers['User-Agent'] \
    and 'X-Gitea-Event' in request.headers and request.headers['X-Gitea-Event'] == 'push':
        return False, {'error': "This appears to be a Nagios ping for service availability testing."}

    # Bail if this is not from DCS
    if 'X-Gitea-Event' not in request.headers:
        logger.error(f"No 'X-Gitea-Event' in {request.headers}")
        return False, {'error': 'This does not appear to be from DCS.'}
    event_type = request.headers['X-Gitea-Event']
    logger.info(f"Got a '{event_type}' event from DCS") # Shows in prodn logs

    # Get the json payload and check it
    payload_json = request.get_json()
    logger.info(f"Webhook payload is {payload_json}")
    # Typical keys are: secret, ref, before, after, compare_url,
    #                               commits, (head_commit), repository, pusher, sender
    # logger.debug("Webhook payload:")
    # for payload_key, payload_entry in payload_json.items():
    #     logger.debug(f"  {payload_key}: {payload_entry!r}")

    # Bail if this is not a push, release (tag), or delete (branch) event
    #   Others include 'create', 'pull_request', 'fork'
    valid_events = {
        "repository": [
            {
                "payload_key": "action",
                "payload_value": "created",
                "verbage": "created a repository"
            },
            {
                "payload_key": "action",
                "payload_value": "deleted",
                "verbage": "deleted a repository"
            },
        ],
        "push": [
            {
                "payload_key": "after",
                "payload_value": None,
                "verbage": "push commits",
            },
        ],
        "delete": [
            {
                "payload_key": "ref_type",
                "payload_value": "branch",
                "verbage": "deleted a branch",
            },
            {
                "payload_key": "ref_type",
                "payload_value": "tag",
                "verbage": "deleted a tag",
            },
        ],
        "fork": [
            {
                "payload_key": "forkee",
                "payload_value": None,
                "verbage": "forked the repo"
            },
        ],
        "release": [
            {
                "payload_key": "action",
                "payload_value": "published",
                "verbage": "published a release"
            },
            {
                "payload_key": "action",
                "payload_value": "updated",
                "verbage": "updated a release"
            },
            {
                "payload_key": "action",
                "payload_value": "deleted",
                "verbage": "deleted a release"
            },
        ],
        "pdf_request": [
            {
                "payload_key": "after",
                "payload_value": None,
                "verbage": "generate a PDF"
            }
        ],
    }
    if event_type not in valid_events:
        message = f"X-Gitea-Event '{event_type}' must be an event of type: {', '.join(valid_events.keys())}"
        logger.error(message)
        logger.info(f"Ignoring '{event_type}' payload: {payload_json}") # Also shows in prodn logs
        return False, {'error': message}
    my_event = None
    payload_keys = []
    payload_values = []
    for event in valid_events[event_type]:
        payload_keys.append(event["payload_key"])
        if "payload_value" in event and event["payload_value"]:
            payload_values.append(event["payload_value"])
        if event["payload_key"] in payload_json and (event["payload_value"] == None or event["payload_value"] == payload_json[event["payload_key"]]):
            my_event = event
            break
    if not my_event:
        message = f"X-Gitea-Event '{event_type}' must have the following properties: {', '.join(payload_keys)}"
        if len(payload_values) > 0:
            message += f" and the following values: {', '.join(payload_values)}"
        logger.error(message)
        logger.info(f"Ignoring '{event_type}' payload: {payload_json}") # Also shows in prodn logs
        return False, {'error': message}
    our_event_verbage = my_event["verbage"]

    # Give a brief but helpful info message for the logs
    try:
        repo_name = payload_json['repository']['full_name']
    except (KeyError, AttributeError):
        repo_name = None
    try:
        pusher_username = payload_json['pusher']['username']
    except (KeyError, AttributeError):
        pusher_username = None
    try:
        sender_username = payload_json['sender']['username']
    except (KeyError, AttributeError):
        sender_username = None

    # Don't process known code repos (cf. content)
    try:
        repo_owner_username = payload_json['repository']['owner']['username']
    except (KeyError, AttributeError):
        repo_owner_username = None
    for unwanted_repo_username in UNWANTED_REPO_OWNER_USERNAMES:
        if unwanted_repo_username == repo_owner_username:
            logger.info(f"Ignoring {event_type} for black-listed \"non-content\" '{unwanted_repo_username}' repo: {repo_name}") # Shows in prodn logs
            return False, {'error': f'This {event_type} appears to be for a "non-content" (program code?) repo.'}


    # Bail if the repo is private
    try:
        private_flag = payload_json['repository']['private']
    except (KeyError, AttributeError):
        private_flag = 'MISSING'
    if private_flag != False:
        logger.error(f"The repo for {event_type} is not public: got {private_flag}")
        return False, {'error': f'The repo for {event_type} is not public.'}


    commit_messages:List[str] = []
    commit_message:Optional[str]
    try:
        # Assemble a string of commit messages
        for commit_dict in payload_json['commits']:
            this_commit_message = commit_dict['message'].strip() # Seems to always end with a newline
            commit_messages.append(f'"{this_commit_message}"')
        commit_message = ', '.join(commit_messages)
    except (KeyError, AttributeError, TypeError, IndexError):
        commit_message = None

    try:
        count_info = 'one commit' if len(commit_messages)==1 else f'{len(commit_messages)} commits'
        extra_info = f" with {count_info}: {commit_message}" if event_type=='push' \
                    else f" with '{payload_json['release']['name']}'"
    except (KeyError, AttributeError):
        extra_info = ""
    if pusher_username:
        logger.info(f"'{pusher_username}' {our_event_verbage} '{repo_name}'{extra_info}")
    elif sender_username:
        logger.info(f"'{sender_username}' {our_event_verbage} '{repo_name}'{extra_info}")
    elif repo_name:
        logger.info(f"UNKNOWN {our_event_verbage} '{repo_name}'{extra_info}")
    else: # they were all None
        logger.info(f"No pusher/sender/repo name in {event_type} ({our_event_verbage}); payload: {payload_json}")

    # Bail if the URL to the repo is invalid
    try:
        if RESTRICT_DCS_URL and not payload_json['repository']['html_url'].startswith(DCS_URL):
            logger.error(f"The repo for {event_type} at '{payload_json['repository']['html_url']}' does not belong to '{DCS_URL}'")
            return False, {'error': f'The repo for {event_type} does not belong to {DCS_URL}.'}
    except KeyError:
        logger.error("No repo URL specified")
        return False, {'error': f"No repo URL specified for {event_type}."}

    if event_type == 'push':
        # Bail if this is not an actual commit
        # NOTE: What are these notifications??? 'before' and 'after' have the same commit id 
        #   Even test/fake deliveries usually have a commit specified (even if after==before)
        #   RESPONSE: This is not always true! A new branch creates a push that has no before commit ID (just 0000000000000000000000000000000000000000)
        #             Going to allow a build if before/after do not match - RHM
        try:
            if not payload_json['commits'] and payload_json['before'] == payload_json['after']:
                logger.error("No commits found for push")
                try: # Just display BEFORE & AFTER for interest if they exist
                    logger.debug(f"BEFORE is {payload_json['before']}")
                    logger.debug(f"AFTER  is {payload_json['after']}")
                except KeyError:
                    pass
                return False, {'error': "No commits found for push."}
        except KeyError:
            logger.error("No commits specified for push")
            return False, {'error': "No commits specified for push."}

    if 'action' in payload_json:
        logger.info(f"This {event_type} has ACTION='{payload_json['action']}'")
    if 'release' in payload_json:
        if 'draft' in payload_json['release'] and payload_json['release']['draft']:
            logger.error(f"This release appears to be a DRAFT {event_type}")
            return False, {'error': f"Preview {event_type} pages don't get built for drafts."}

    # Add the event to the payload to be passed on
    payload_json['DCS_event'] = event_type
    if 'X-Gitea-Deliver' in request.headers and request.headers['X-Gitea-Delivery']:
        payload_json['job_id'] = request.headers['X-Gitea-Delivery']

    logger.debug(f"Door43 payload for {event_type} seems ok")
    return True, payload_json
# end of check_posted_payload


def check_posted_callback_payload(request, logger) -> Tuple[bool, Dict[str,Any]]:
    """
    Accepts callback notification from tX-Job-Handler.
        Parameter is a rq request object

    Returns a 2-tuple:
        True or False if payload checks out
        Either the payload that was checked (if returning True above),
            or the error dict (if returning False above)
    """
    # Bail if this is not a POST with a payload
    if not request.data:
        logger.error("Received request but no payload found")
        return False, {'error': 'No payload found. You must submit a POST request.'}

    # Get the json payload and check it
    callback_payload_json = request.get_json()
    logger.debug(f"Callback payload is {callback_payload_json}") # Doesn't show in main logs

    if 'job_id' not in callback_payload_json or not callback_payload_json['job_id']:
        logger.error("No callback job_id specified")
        return False, {'error': "No callback job_id specified."}

    # Display some helpful info in the logs
    if 'status' in callback_payload_json and 'identifier' in callback_payload_json and callback_payload_json['identifier']:
        logger.info(f"Received '{callback_payload_json['status']}' callback for {callback_payload_json['identifier']}")
    if 'linter_warnings' in callback_payload_json and 'linter_success' in callback_payload_json:
        logger.info(f"linter_success={callback_payload_json['linter_success']} with {len(callback_payload_json['linter_warnings'])} warnings")
    if 'success' in callback_payload_json and 'converter_warnings' in callback_payload_json and 'converter_errors' in callback_payload_json:
        logger.info(f"success={callback_payload_json['success']} with {len(callback_payload_json['converter_errors'])} converter errors and {len(callback_payload_json['converter_warnings'])} warnings")

    logger.debug("Door43 callback payload seems ok")
    return True, callback_payload_json
# end of check_posted_callback_payload
