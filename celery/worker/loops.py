"""
celery.worker.loop
~~~~~~~~~~~~~~~~~~

The consumers highly-optimized inner loop.

"""
from __future__ import absolute_import

import socket

from celery.bootsteps import RUN
from celery.exceptions import SystemTerminate, WorkerLostError
from celery.utils.log import get_logger

from . import state

__all__ = ['asynloop', 'synloop']

logger = get_logger(__name__)
error = logger.error


def asynloop(obj, connection, consumer, blueprint, hub, qos,
             heartbeat, clock, hbrate=2.0, RUN=RUN):
    """Non-blocking event loop consuming messages until connection is lost,
    or shutdown is requested."""

    update_qos = qos.update
    readers, writers = hub.readers, hub.writers
    hbtick = connection.heartbeat_check
    errors = connection.connection_errors
    hub_add, hub_remove = hub.add, hub.remove

    on_task_received = obj.create_task_handler()

    if heartbeat and connection.supports_heartbeats:
        hub.call_repeatedly(heartbeat / hbrate, hbtick, hbrate)

    consumer.callbacks = [on_task_received]
    consumer.consume()
    obj.on_ready()
    obj.controller.register_with_event_loop(hub)
    obj.register_with_event_loop(hub)

    # did_start_ok will verify that pool processes were able to start,
    # but this will only work the first time we start, as
    # maxtasksperchild will mess up metrics.
    if not obj.restart_count and not obj.pool.did_start_ok():
        raise WorkerLostError('Could not start worker processes')
    loop = hub._loop(propagate=errors)

    try:
        while blueprint.state == RUN and obj.connection:
            # shutdown if signal handlers told us to.
            if state.should_stop:
                raise SystemExit()
            elif state.should_terminate:
                raise SystemTerminate()

            # We only update QoS when there is no more messages to read.
            # This groups together qos calls, and makes sure that remote
            # control commands will be prioritized over task messages.
            if qos.prev != qos.value:
                update_qos()

            try:
                next(loop)
            except StopIteration:
                loop = hub._loop(propagate=errors)
    finally:
        try:
            hub.close()
        except Exception as exc:
            error(
                'Error cleaning up after event loop: %r', exc, exc_info=1,
            )


def synloop(obj, connection, consumer, blueprint, hub, qos,
            heartbeat, clock, hbrate=2.0, **kwargs):
    """Fallback blocking event loop for transports that doesn't support AIO."""

    on_task_received = obj.create_task_handler()
    consumer.register_callback(on_task_received)
    consumer.consume()

    obj.on_ready()

    while blueprint.state == RUN and obj.connection:
        state.maybe_shutdown()
        if qos.prev != qos.value:
            qos.update()
        try:
            connection.drain_events(timeout=2.0)
        except socket.timeout:
            pass
        except socket.error:
            if blueprint.state == RUN:
                raise