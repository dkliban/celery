import socket

from celery.app import app_or_default
from celery.pidbox import mailbox
from celery.utils import kwdict
from celery.worker.control.registry import Panel

__import__("celery.worker.control.builtins")


class ControlDispatch(object):
    """Execute worker control panel commands."""
    Panel = Panel

    def __init__(self, logger=None, hostname=None, consumer=None, app=None,
            channel=None):
        self.app = app_or_default(app)
        self.logger = logger or self.app.log.get_default_logger()
        self.hostname = hostname or socket.gethostname()
        self.consumer = consumer
        self.channel = channel
        self.panel = self.Panel(self.logger, self.consumer, self.hostname,
                                app=self.app)

    def reply(self, data, exchange, routing_key, **kwargs):

        def _do_reply(connection=None, connect_timeout=None):
            mailbox(connection).publish_reply(data, exchange, routing_key,
                                              channel=self.channel)

        self.app.with_default_connection(_do_reply)(**kwargs)

    def dispatch_from_message(self, message):
        """Dispatch by using message data received by the broker.

        Example:

            >>> def receive_message(message_data, message):
            ...     control = message_data.get("control")
            ...     if control:
            ...         ControlDispatch().dispatch_from_message(control)

        """
        message = dict(message)             # don't modify callers message.
        command = message.pop("command")
        destination = message.pop("destination", None)
        reply_to = message.pop("reply_to", None)
        if not destination or self.hostname in destination:
            return self.execute(command, message, reply_to=reply_to)

    def execute(self, command, kwargs=None, reply_to=None):
        """Execute control command by name and keyword arguments.

        :param command: Name of the command to execute.
        :param kwargs: Keyword arguments.

        """
        kwargs = kwargs or {}
        control = None
        try:
            control = self.panel[command]
        except KeyError:
            self.logger.error("No such control command: %s" % command)
        else:
            try:
                reply = control(self.panel, **kwdict(kwargs))
            except SystemExit:
                raise
            except Exception, exc:
                self.logger.error(
                        "Error running control command %s kwargs=%s: %s" % (
                            command, kwargs, exc))
                reply = {"error": str(exc)}
            if reply_to:
                self.reply({self.hostname: reply},
                           exchange=reply_to["exchange"],
                           routing_key=reply_to["routing_key"])
            return reply
