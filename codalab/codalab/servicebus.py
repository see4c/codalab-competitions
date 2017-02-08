import logging
import time
from django.conf import settings

from codalabtools import (
    Queue,
    QueueMessage)

from codalabtools.azure_extensions import AzureServiceBusQueue
import redis

logger = logging.getLogger(__name__)

def get_default_servicebus_queue(name):

    queue = None

    if settings.DEFAULT_SERVICE_BUS == 'Azure':
        queue = AzureServiceBusQueue(settings.SBS_NAMESPACE,
                                     settings.SBS_ACCOUNT_KEY,
                                     settings.SBS_ISSUER,
                                     name)
    elif settings.DEFAULT_SERVICE_BUS == 'Redis':
        queue = RedisServiceBusQueue(settings.REDIS_HOST,
                                     settings.REDIS_PORT,
                                     0,
                                     name)

    return queue


class RedisServiceBusQueueMessage(QueueMessage):
    """
    Implements a QueueMessage backed by Redis.
    """
    def __init__(self, queue, message):
        self.queue = queue
        self.message = message
    def get_body(self):
        return self.message.body
    def get_queue(self):
        raise self.queue

class RedisServiceBusQueue(Queue):
    """
    Implements a Queue backed by Redis.
    """

    # Timeout in seconds. receive_message is blocking and returns as soon as one of two
    # conditions occurs: a message is received or the timeout period has elapsed.
    polling_timeout = 60

    def __init__(self, host, port, db, name):
        self.service = redis.StrictRedis(host=host, port=port, db=db)

        self.name = name
        self.max_retries = 3
        self.wait = lambda count: 1.0*(2**count)

    def receive_message(self):
        retry_count = 0
        while True:
            retry_count += 1
            msg = self.service.get(self.name)
            if msg or retry_count == self.max_retries:
                return None if msg.body is None else RedisServiceBusQueueMessage(self, msg)
            wait_interval = self.wait(retry_count)
            if wait_interval > 0.0:
                time.sleep(wait_interval)


    def send_message(self, body):
        self.service.set(self.name, body)







