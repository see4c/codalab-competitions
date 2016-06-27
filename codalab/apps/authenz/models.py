from django.db import models
from django.contrib.auth import models as auth_models


class ClUser(auth_models.AbstractUser):
    # Notification settings
    participation_status_updates = models.BooleanField(default=True)
    organizer_status_updates = models.BooleanField(default=True)
    organizer_direct_message_updates = models.BooleanField(default=True)
    email_on_submission_finished_successfully = models.BooleanField(default=False)

    # Profile details
    organization_or_affiliation = models.CharField(max_length=255, null=True, blank=True)
    biography = models.TextField(null=True, blank=True)
    webpage = models.URLField(null=True, blank=True)
    linkedin = models.CharField(max_length=255, null=True, blank=True)
    ORCID = models.CharField(max_length=255, null=True, blank=True)

    team_name = models.CharField(max_length=64, null=True, blank=True)
    team_members = models.TextField(null=True, blank=True)

    method_name = models.CharField(max_length=20, null=True, blank=True)
    method_description = models.TextField(null=True, blank=True)
    project_url = models.URLField(null=True, blank=True)
    publication_url = models.URLField(null=True, blank=True)
    bibtex = models.TextField(null=True, blank=True)

    contact_email = models.EmailField(null=True, blank=True)

    public_profile = models.BooleanField(default=False)
