import logging
import os
from django.core.files.storage import Storage
from django.conf import settings
from django.http import HttpResponse
from azure_storage import AzureStorage as CAzureStorage, make_blob_sas_url, PREFERRED_STORAGE_X_MS_VERSION
from django.core.files.storage import FileSystemStorage
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.core.servers.basehttp import FileWrapper
from django.core.files.storage import get_storage_class

logger = logging.getLogger(__name__)

# keep consistent path separators
pathjoin = lambda *args: os.path.join(*args).replace("\\", "/")


def clean_name(name):
    return os.path.normpath(name).replace("\\", "/")

class ExtFileSystemStorage(FileSystemStorage):
    def __init__(self, *args, **kwargs):
        super(ExtFileSystemStorage, self).__init__(*args, **kwargs)

    def make_blob_sas_url(self, blob_name, permission, duration):
        url = 'http://{0}:{1}/storage/{2}/{3}'.format(settings.SERVER_NAME, settings.PORT, 'bundle', blob_name)
        return url

    @property
    def preferred_version(self):
        return ''

class DefaultStorage(Storage):
    UNKNOWN = 'unknown'
    PUBLIC = 'public'
    BUNDLE = 'bundle'

    @property
    def connection(self):
        return None

    @property
    def preferred_version(self):
        return ''

    def make_blob_sas_url(self, blob_name, permission='w', duration=16):
        return None

    def __init__(self, *args, **kwargs):
        self.bundle = None
        self.public = None
        self.default = self.public
        super(DefaultStorage, self).__init__(*args, **kwargs)

    def get_storage(self, storage_type):
        if storage_type == self.PUBLIC:
            return self.public
        elif storage_type == self.BUNDLE:
            return self.bundle
        else:
            return None

    def set_default(self, storage_type):
        self.default = storage_type

    def open(self, name, mode='rb'):
        return self.get_storage(self.default).open(name, mode)

    def save(self, name, content):
        return self.get_storage(self.default).save(name, content)

    def get_valid_name(self, name):
        return self.get_storage(self.default).get_valid_name(name)

    def get_available_name(self, name):
        return self.get_storage(self.default).get_available_name(name)

    def path(self, name):
        return self.get_storage(self.default).path(name)

    def delete(self, name):
        return self.get_storage(self.default).delete(name)

    def exists(self, name):
        return self.get_storage(self.default).exists(name)

    def listdir(self, path):
        return self.get_storage(self.default).listdir(path)

    def size(self, name):
        return self.get_storage(self.default).size(name)

    def url(self, name):
        return self.get_storage(self.default).url(name)

    def accessed_time(self, name):
        return self.get_storage(self.default).accessed_time(name)

    def created_time(self, name):
        return self.get_storage(self.default).created_time(name)

    def modified_time(self, name):
        return self.get_storage(self.default).modified_time(name)


class LocalStorage(DefaultStorage):
    def __init__(self, *args, **kwargs):
        super(LocalStorage, self).__init__(*args, **kwargs)

        bundle_location = os.path.join(settings.MEDIA_ROOT, 'bundle')
        bundle_url = 'http://{0}:{1}{2}{3}/'.format(settings.SERVER_NAME, settings.PORT, settings.MEDIA_URL, 'bundle')

        public_location = os.path.join(settings.MEDIA_ROOT, 'public')
        public_url = 'http://{0}:{1}{2}{3}/'.format(settings.SERVER_NAME, settings.PORT, settings.MEDIA_URL, 'public')

        if not os.path.exists(bundle_location):
            os.makedirs(bundle_location)

        if not os.path.exists(public_location):
            os.makedirs(public_location)

        self.bundle = ExtFileSystemStorage(location=bundle_location, base_url=bundle_url)
        self.public = ExtFileSystemStorage(location=public_location, base_url=public_url)

        logger.info('Using Local storage')



    def make_blob_sas_url(self, blob_name, permission, duration):
        return '{0}{1}'.format(self.bundle.base_url, blob_name)


class AzureStorage(DefaultStorage):
    def __init__(self, *args, **kwargs):
        super(DefaultStorage, self).__init__(*args, **kwargs)
        try:
            self.bundle = CAzureStorage(account_name=settings.BUNDLE_AZURE_ACCOUNT_NAME,
                                       account_key=settings.BUNDLE_AZURE_ACCOUNT_KEY,
                                       azure_container=settings.BUNDLE_AZURE_CONTAINER)

            self.public = CAzureStorage(account_name=settings.AZURE_ACCOUNT_NAME,
                                       account_key=settings.AZURE_ACCOUNT_KEY,
                                       azure_container=settings.AZURE_CONTAINER)

            logger.info('Using Azure storage')

        except Exception as ex:
            logger.error('Cannot create Azure storage connections. Check Local configuration.')

    @property
    def connection(self):
        return self.get_storage(self.default)._connection

    @property
    def preferred_version(self):
        return PREFERRED_STORAGE_X_MS_VERSION

    def make_blob_sas_url(account_name,
                          account_key,
                          container_name,
                          blob_name,
                          permission='w',
                          duration=16):
        return make_blob_sas_url(account_key, account_key)


@api_view(['GET', 'POST', 'PUT', 'DELETE'])
def storage_api(request, blob_name, path):

    StorageClass = get_storage_class(settings.DEFAULT_FILE_STORAGE)

    StorageObj = StorageClass.get_storage(blob_name)

    if request.method == 'GET':
        rel_path, filename = os.path.split(path)
        zip_file = open(path, 'rb')
        response = HttpResponse(FileWrapper(zip_file), content_type='application/zip')
        response['Content-Disposition'] = 'attachment; filename="%s"' % filename
        return response

    elif request.method == 'POST':
        #serializer = SnippetSerializer(data=request.data)
        #if serializer.is_valid():
        #    serializer.save()
        #    return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(status=status.HTTP_400_BAD_REQUEST)


