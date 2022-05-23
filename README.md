master:
G
[![Build Status](https://travis-ci.org/unfoldingWord-dev/door43-enqueue-job.svg?branch=master)](https://travis-ci.org/unfoldingWord-dev/door43-enqueue-job?branch=master)
[![Coverage Status](https://coveralls.io/repos/github/unfoldingWord-dev/door43-enqueue-job/badge.svg?branch=master)](https://coveralls.io/github/unfoldingWord-dev/door43-enqueue-job?branch=master)

develop:
[![Build Status](https://travis-ci.org/unfoldingWord-dev/door43-enqueue-job.svg?branch=develop)](https://travis-ci.org/unfoldingWord-dev/door43-enqueue-job?branch=develop)
[![Coverage Status](https://coveralls.io/repos/github/unfoldingWord-dev/door43-enqueue-job/badge.svg?branch=develop)](https://coveralls.io/github/unfoldingWord-dev/door43-enqueue-job?branch=develop)

# Door43-Enqueue-Job

This is part of tX translationConverter platform initiated by a commit to the
DCS (Door43 Content Service) at [door43.org](https://door43.org/).

See [here](https://forum.ccbt.bible/t/door43-org-tx-development-architecture/65)
for a diagram of the overall flow of the tx (translationConverter) platform.

That is more up-to-date than the write-up of the previous platform
[here](https://github.com/unfoldingWord-dev/door43.org/wiki/tX-Development-Architecture)
(which was too dependent on expensive AWS lambda functions).


## Door43 modifications

Modified June 2018 by RJH mainly to add vetting of the json payload from DCS
before the job is added to the redis queue.

Also added Graphite stats collection (using statsd package).
and viewable with Grafana.

See the `Makefile` for a list of environment variables which are looked for.

```
Requires:
    Linux
    Python 3.6 (or later)

To setup:
    python3 -m venv myVenv
    source myVenv/bin/activate
    make dependencies

Set environment variables:
    (see enqueue/Makefile for a list of expected and optional environment variables)
    e.g., export QUEUE_PREFIX="dev-"

To try Python code in Flask:
    make runFlask
    (then can post data to http://127.0.0.1:5000/
        if there is no redis instance running)

To run (using Flask and gunicorn and nginx, plus redis) in three docker containers:
    make composeEnqueueRedis
    (then send json payload data to http://127.0.0.1:8080/)

To build a docker image:
    (requires environment variable DOCKER_USERNAME to be set)
    make image

To push the image to docker hub:
    docker login -u <dockerUsername>
    make pushImage
```

Basically this small program collects the json payload from the DCS (Door43
Content Service) which connects to the `/` URL.)

This enqueue process checks for various fields for simple validation of the
payload, and then puts the job onto a (rq) queue (stored in redis) to be
processed.

There is also a callback service connected to the `tx-callback` URL.
Callback jobs are placed onto a different queue.

The Python code is run in Flask, which is then served by Green Unicorn (gunicorn)
but with nginx facing the outside world.

## Testing

Use `make composeEnqueueRedis` as above.
The door43_job_handler also needs to be running.
Use a command like `curl -v http://127.0.0.1:8080/ -d @<path-to>/payload.json --header "Content-Type: application/json" --header "X-Gitea-Event: push"` to queue a job, and if successful, you should receive a JSON response.


## Deployment

Travis-CI is hooked to from GitHub to automatically test commits to both the `develop`
and `master` branches, and on success, to build containers (tagged with those branch names)
that are pushed to [DockerHub](https://hub.docker.com/u/unfoldingword/).

```
To fetch the container use something like:
    docker pull --all-tags unfoldingword/door43_enqueue_job
or
    docker pull unfoldingword/door43_enqueue_job:develop

To view downloaded images and their tags:
    docker images

To test the container use:
    docker run --env QUEUE_PREFIX="dev-" --env FLASK_ENV="development" --env REDIS_HOSTNAME=<redis_hostname> --net="host" --name dev-door43_enqueue_job --rm unfoldingword/door43_enqueue_job:develop

or alternatively also including the optional Graphite url:
    docker run --env QUEUE_PREFIX="dev-" --env FLASK_ENV="development" --env REDIS_HOSTNAME=<redis_hostname> --env GRAPHITE_HOSTNAME=<graphite_hostname> --net="host" --name dev-door43_enqueue_job --rm unfoldingword/door43_enqueue_job:develop

NOTE: --rm automatically removes the container from the docker daemon when it exits
            (it doesn't delete the pulled image from disk)

To run the container in production use with the desired values:
    docker run --env GRAPHITE_HOSTNAME=<graphite_hostname> --env REDIS_HOSTNAME=<redis_hostname> --net="host" --name door43_enqueue_job --detach --rm unfoldingword/door43_enqueue_job:master

Running containers can be viewed with (or append --all to see all containers):
    docker ps

You can connect to a shell inside the container with commands like:
	# Gives a shell on the running container -- Note: no bash shell available
	docker exec -it `docker inspect --format="{{.Id}}" tx_enqueue_job` sh
	docker exec -it `docker inspect --format="{{.Id}}" dev-tx_enqueue_job` sh

The container can be stopped with a command like:
    docker stop dev-door43_enqueue_job
or using the full container name:
    docker stop unfoldingword/door43_enqueue_job:develop
```

The production container will be deployed to the unfoldingWord AWS EC2 instance, where
[Watchtower](https://github.com/v2tec/watchtower) will automatically check for, pull, and run updated containers.

## Further processing

The next part in the Door43 workflow can be found in the [door43-job-handler](https://github.com/unfoldingWord-dev/door43-job-handler)
repo. The job handler contains `webhook.py` (see below) which is given jobs
that have been removed from the queue and then processes them -- adding them
back to a `failed` queue if they give an exception or time-out.

The `callback.py` module handles the callbacks from the tX (Translation Converter)
and places them in a separate (higher priority) queue.

Note that the queue names here in `enqueueMain.py` must match the ones
in the Door43 job handler `rq_settings.py`.


# The following is the initial (forked) README
# Webhook job queue
The webhook job queue is designed to receive notifications from services and
store the JSON as a dictionary in a `python-rq` redis queue. It is designed
to work with a [Webhook Relay](https://github.com/lscsoft/webhook-relay) that
validates and relays webhooks from known services such as DockerHub, Docker
registries, GitHub, and GitLab. However, this is not required and the receiver
may listen directly to these services.

A worker must be spawned separately to read from the queue and perform tasks in
response to the event. The worker must have a function named `webhook.job`.

## Running

The job queue requires [docker-compose](https://docs.docker.com/compose/install/)
and, in its simplest form, can be invoked with `docker-compose up`. By default,
it will bind to `localhost:8080` but allow clients from all IP addresses. This
may appear odd, but on MacOS and Windows, traffic to the containers will appear
as though it's coming from the gateway of the network created by
Docker's linux virtualization.

In a production environment without the networking restrictions imposed by
MacOS/Windows, you might elect to provide different defaults through the
the shell environment. _e.g._
```
ALLOWED_IPS=A.B.C.D LISTEN_IP=0.0.0.0 docker-compose up
```
where `A.B.C.D` is an IP address (or CIDR range) from which your webhooks will
be sent.

A [worker must be spawned](#example-worker) to perform tasks by removing the
notification data from the redis queue. The redis keystore is configured to
listen only to clients on the `localhost`.

## Example worker
To run jobs using the webhooks as input:

1. Create a file named `webhook.py`
2. Define a function within named `job` that takes a `dict` as its lone argument
3. Install `python-rq`
    * _e.g._ `pip install rq`
4. Run `rq worker` from within that directory

See the [CVMFS-to-Docker converter](https://github.com/lscsoft/cvmfs-docker-worker)
for a real world example.
