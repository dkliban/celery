
from datetime import datetime, timedelta

from carrot.connection import BrokerConnection
from carrot import messaging

from celery import routes
from celery import signals
from celery.utils import gen_unique_id, mitemgetter, textindent


MSG_OPTIONS = ("mandatory", "priority", "immediate",
               "routing_key", "serializer", "delivery_mode")
QUEUE_FORMAT = """
. %(name)s -> exchange:%(exchange)s (%(exchange_type)s) \
binding:%(binding_key)s
"""
BROKER_FORMAT = "%(carrot_backend)s://%(userid)s@%(host)s%(port)s%(vhost)s"

get_msg_options = mitemgetter(*MSG_OPTIONS)
extract_msg_options = lambda d: dict(zip(MSG_OPTIONS, get_msg_options(d)))


_queues_declared = False
_exchanges_declared = set()




class TaskPublisher(messaging.Publisher):
    auto_declare = False

    def declare(self):
        if self.exchange not in _exchanges_declared:
            super(TaskPublisher, self).declare()
            _exchanges_declared.add(self.exchange)

    def delay_task(self, task_name, task_args=None, task_kwargs=None,
            countdown=None, eta=None, task_id=None, taskset_id=None,
            expires=None, exchange=None, exchange_type=None, **kwargs):
        """Delay task for execution by the celery nodes."""

        task_id = task_id or gen_unique_id()
        task_args = task_args or []
        task_kwargs = task_kwargs or {}
        now = None
        if countdown: # Convert countdown to ETA.
            now = datetime.now()
            eta = now + timedelta(seconds=countdown)

        if not isinstance(task_args, (list, tuple)):
            raise ValueError("task args must be a list or tuple")
        if not isinstance(task_kwargs, dict):
            raise ValueError("task kwargs must be a dictionary")

        if isinstance(expires, int):
            now = now or datetime.now()
            expires = now + timedelta(seconds=expires)

        message_data = {
            "task": task_name,
            "id": task_id,
            "args": task_args or [],
            "kwargs": task_kwargs or {},
            "retries": kwargs.get("retries", 0),
            "eta": eta and eta.isoformat(),
            "expires": expires and expires.isoformat(),
        }

        if taskset_id:
            message_data["taskset"] = taskset_id

        # custom exchange passed, need to declare it.
        if exchange and exchange not in _exchanges_declared:
            exchange_type = exchange_type or self.exchange_type
            self.backend.exchange_declare(exchange=exchange,
                                          exchange_type=exchange_type,
                                          durable=self.durable,
                                          auto_delete=self.auto_delete)
        self.send(message_data, exchange=exchange,
                  **extract_msg_options(kwargs))
        signals.task_sent.send(sender=task_name, **message_data)

        return task_id


class ConsumerSet(messaging.ConsumerSet):
    """ConsumerSet with an optional decode error callback.

    For more information see :class:`carrot.messaging.ConsumerSet`.

    .. attribute:: on_decode_error

        Callback called if a message had decoding errors.
        The callback is called with the signature::

            callback(message, exception)

    """
    on_decode_error = None

    def _receive_callback(self, raw_message):
        message = self.backend.message_to_python(raw_message)
        if self.auto_ack and not message.acknowledged:
            message.ack()
        try:
            decoded = message.decode()
        except Exception, exc:
            if self.on_decode_error:
                return self.on_decode_error(message, exc)
            else:
                raise
        self.receive(decoded, message)


class AMQP(object):
    BrokerConnection = BrokerConnection
    Publisher = messaging.Publisher
    Consumer = messaging.Consumer
    ConsumerSet = ConsumerSet

    def __init__(self, app):
        self.app = app

    def get_queues(self):
        c = self.app.conf
        queues = c.CELERY_QUEUES

        def _defaults(opts):
            opts.setdefault("exchange", c.CELERY_DEFAULT_EXCHANGE),
            opts.setdefault("exchange_type", c.CELERY_DEFAULT_EXCHANGE_TYPE)
            opts.setdefault("binding_key", c.CELERY_DEFAULT_EXCHANGE)
            opts.setdefault("routing_key", opts.get("binding_key"))
            return opts

        return dict((queue, _defaults(opts))
                    for queue, opts in queues.items())

    def get_default_queue(self):
        q = self.app.conf.CELERY_DEFAULT_QUEUE
        return q, self.get_queues()[q]

    def Router(self, queues=None, create_missing=None):
        return routes.Router(self.app.conf.CELERY_ROUTES,
                             queues or self.app.conf.CELERY_QUEUES,
                             self.app.either("CELERY_CREATE_MISSING_QUEUES",
                                             create_missing))

    def TaskConsumer(self, *args, **kwargs):
        default_queue_name, default_queue = self.get_default_queue()
        defaults = dict({"queue": default_queue_name}, **default_queue)
        defaults["routing_key"] = defaults.pop("binding_key", None)
        return self.Consumer(*args,
                             **self.app.merge(defaults, kwargs))

    def TaskPublisher(self, *args, **kwargs):
        _, default_queue = self.get_default_queue()
        defaults = {"exchange": default_queue["exchange"],
                    "exchange_type": default_queue["exchange_type"],
                    "routing_key": self.app.conf.CELERY_DEFAULT_ROUTING_KEY,
                    "serializer": self.app.conf.CELERY_TASK_SERIALIZER}
        publisher = TaskPublisher(*args,
                                  **self.app.merge(defaults, kwargs))

        # Make sure all queues are declared.
        global _queues_declared
        if not _queues_declared:
            consumers = self.get_consumer_set(publisher.connection)
            consumers.close()
            _queues_declared = True
        publisher.declare()

        return publisher

    def get_consumer_set(self, connection, queues=None, **options):
        queues = queues or self.get_queues()

        cset = self.ConsumerSet(connection)
        for queue_name, queue_options in queues.items():
            queue_options = dict(queue_options)
            queue_options["routing_key"] = queue_options.pop("binding_key",
                                                             None)
            consumer = self.Consumer(connection, queue=queue_name,
                                     backend=cset.backend, **queue_options)
            cset.consumers.append(consumer)
        return cset

    def format_queues(self, queues, indent=0):
        """Format routing table into string for log dumps."""
        format = lambda **queue: QUEUE_FORMAT.strip() % queue
        info = "\n".join(format(name=name, **config)
                                for name, config in queues.items())
        return textindent(info, indent=indent)

    def get_broker_info(self):
        broker_connection = self.app.broker_connection()
        carrot_backend = broker_connection.backend_cls
        if carrot_backend and not isinstance(carrot_backend, str):
            carrot_backend = carrot_backend.__name__
        carrot_backend = carrot_backend or "amqp"

        port = broker_connection.port or \
                    broker_connection.get_backend_cls().default_port
        port = port and ":%s" % port or ""

        vhost = broker_connection.virtual_host
        if not vhost.startswith("/"):
            vhost = "/" + vhost

        return {"carrot_backend": carrot_backend,
                "userid": broker_connection.userid,
                "host": broker_connection.hostname,
                "port": port,
                "vhost": vhost}

    def format_broker_info(self, info=None):
        """Get message broker connection info string for log dumps."""
        return BROKER_FORMAT % self.get_broker_info()
