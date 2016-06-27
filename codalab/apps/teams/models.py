import logging
from django.conf import settings
from django.core.files.storage import get_storage_class
from django.db import models
from django.utils.timezone import now
from django.utils.functional import cached_property

User = settings.AUTH_USER_MODEL
logger = logging.getLogger(__name__)

## Needed for computation service handling
## Hack for now
StorageClass = get_storage_class(settings.DEFAULT_FILE_STORAGE)
try:
    BundleStorage = StorageClass(account_name=settings.BUNDLE_AZURE_ACCOUNT_NAME,
                                        account_key=settings.BUNDLE_AZURE_ACCOUNT_KEY,
                                        azure_container=settings.BUNDLE_AZURE_CONTAINER)

    PublicStorage = StorageClass(account_name=settings.AZURE_ACCOUNT_NAME,
                                        account_key=settings.AZURE_ACCOUNT_KEY,
                                        azure_container=settings.AZURE_CONTAINER)

except:
    BundleStorage = StorageClass()
    PublicStorage = StorageClass()


def get_competition_teams(competition):
    team_list=Team.objects.filter(
        competition=competition,
        is_active=True,
    ).all()
    return team_list

def get_competition_user_teams(competition,user):
    team_list=Team.objects.filter(
        competition=competition,
        is_active=True,
        creator=user.user,
    ).all()
    if len(team_list)==0:
        team_list=None
    else:
        team_list=team_list[0]
    return team_list



def get_user_requests(user, competition):
    team_list=get_competition_teams(competition)
    user_requests = TeamMembership.objects.filter(
        user=user,
        team__in=team_list,
    ).select_related('team').all()
    return user_requests

def get_user_team(user, competition):
    team=get_competition_user_teams(competition, user)

    if team is not None:
        return team

    team_list=get_competition_teams(competition)
    user_requests = get_user_requests(user, competition)
    user_team=user_requests.filter(is_accepted=True).all()
    if len(user_team)==0:
        user_team=None

    if user_team is not None:
        for req in user_team:
            if req.is_active:
                team=req

    if team is not None:
        team=team.team

    return team

# Create your models here.
class Team(models.Model):
    """ This is the base team. """
    class Meta:
        unique_together = (('name', 'competition'),)

    def __unicode__(self):
        return "%s - %s" % (self.competition.title, self.name)

    name = models.CharField(max_length=100)
    competition = models.ForeignKey('web.Competition')
    description = models.TextField(null=True, blank=True)
    image = models.ImageField(upload_to='team_logo', storage=PublicStorage, null=True, blank=True, verbose_name="Logo")
    image_url_base = models.CharField(max_length=255)
    allow_requests = models.BooleanField(default=True, verbose_name="Allow requests")
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='team_creator')
    members = models.ManyToManyField(settings.AUTH_USER_MODEL, through='TeamMembership', blank=True, null=True)
    last_modified = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True, verbose_name="Is Active")
    is_accepted = models.BooleanField(default=False, verbose_name="Is Accepted")

    def save(self, *args, **kwargs):
        # Make sure the image_url_base is set from the actual storage implementation
        #self.image_url_base = self.image.storage.url('')
        self.last_modified=now()


        # Do the real save
        return super(Team,self).save(*args,**kwargs)


class TeamMembership(models.Model):
    def __unicode__(self):
        return "%s - %s" % (self.team_id, self.user_id)

    @property
    def is_active(self):
        if self.start_date is not None and now() < self.start_date:
            return False
        if self.end_date is not None and now() > self.end_date:
            return False

        return True

    user = models.ForeignKey(settings.AUTH_USER_MODEL)
    team = models.ForeignKey(Team)
    is_invitation = models.BooleanField(default=False)
    is_request = models.BooleanField(default=False)
    is_accepted = models.BooleanField(default=False)
    start_date = models.DateTimeField(null=True, blank=True, verbose_name="Start Date (UTC)")
    end_date = models.DateTimeField(null=True, blank=True, verbose_name="End Date (UTC)")
    message = models.TextField(null=True, blank=True)

