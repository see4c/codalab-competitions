from django.conf.urls import patterns, include, url
from django.conf.urls.static import static
from django.conf import settings
from django.contrib import admin
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.views.generic import TemplateView
from storage import storage_api

admin.autodiscover()

urlpatterns = patterns('',
    url(r'', include('apps.web.urls')),
    url(r'^accounts/', include('allauth.urls')),
    url(r'^clients/', include('apps.authenz.urls')),
    url(r'^api/', include('apps.api.routers')),
    url(r'^search/', include('haystack.urls')),

    # Uncomment the admin/doc line below to enable admin documentation:
    # url(r'^admin/doc/', include('django.contrib.admindocs.urls')),

    # Uncomment the next line to enable the admin:
    # url(r'^admin/', include(admin.site.urls)),

    # Static files
    url(r'^static/(?P<path>.*)$', 'django.views.static.serve',
        {'document_root': settings.STATIC_ROOT}),

    # Media files
    url(r'^media/(?P<path>.*)$', 'django.views.static.serve',
        {'document_root': settings.MEDIA_ROOT}),

    # JS Reverse for saner AJAX calls
    url(r'^jsreverse/$', 'django_js_reverse.views.urls_js', name='js_reverse'),

    # Local storage service
    url(r'^storage/(?P<blob_name>.*)/(?P<path>.*)$', storage_api)

)
