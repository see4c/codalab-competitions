import csv
import datetime
import django.dispatch
import exceptions
import io
import json
import logging
import operator
import os
import random
import string
import StringIO
import tempfile
import time
import uuid
import yaml
import zipfile

from os.path import abspath, basename, dirname, join, normpath, split

from django.conf import settings
from django.contrib.contenttypes import generic
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.core.files import File
from django.core.files.storage import get_storage_class
from django.core.urlresolvers import reverse, reverse_lazy
from django.db import IntegrityError
from django.db import models
from django.db import transaction
from django.db.models import Max
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify
from django.utils.timezone import now

from mptt.models import MPTTModel, TreeForeignKey

from pytz import utc
from guardian.shortcuts import assign_perm
from django_extensions.db.fields import UUIDField
from django.utils.functional import cached_property

from apps.forums.models import Forum
from apps.coopetitions.models import DownloadRecord

from apps.teams.models import Team, get_user_team


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

# Competition Content
class ContentVisibility(models.Model):
    name = models.CharField(max_length=20)
    codename = models.SlugField(max_length=20,unique=True)
    classname = models.CharField(max_length=30,null=True,blank=True)

    def __unicode__(self):
        return self.name

class ContentCategory(MPTTModel):
    parent = TreeForeignKey('self', related_name='children', null=True, blank=True)
    name = models.CharField(max_length=100)
    codename = models.SlugField(max_length=100,unique=True)
    visibility = models.ForeignKey(ContentVisibility)
    is_menu = models.BooleanField(default=True)
    content_limit = models.PositiveIntegerField(default=1)

    def __unicode__(self):
        return self.name

class DefaultContentItem(models.Model):
    category = TreeForeignKey(ContentCategory)
    label = models.CharField(max_length=100)
    codename = models.SlugField(max_length=100,unique=True)
    rank = models.IntegerField(default=0)
    required = models.BooleanField(default=False)
    initial_visibility =  models.ForeignKey(ContentVisibility)

    def __unicode__(self):
        return self.label

class PageContainer(models.Model):
    name = models.CharField(max_length=200,blank=True)
    content_type = models.ForeignKey(ContentType)
    object_id = models.PositiveIntegerField(db_index=True)
    owner = generic.GenericForeignKey('content_type', 'object_id')

    class Meta:
        unique_together = (('object_id','content_type'),)

    def __unicode__(self):
        return self.name

    def save(self,*args,**kwargs):
        self.name = "%s - %s" % (self.owner.__unicode__(), self.name if self.name else str(self.pk))
        return super(PageContainer,self).save(*args,**kwargs)

# External Files (These might be able to be removed, per a discussion 2013.7.29)
class ExternalFileType(models.Model):
    name = models.CharField(max_length=20)
    codename = models.SlugField(max_length=20,unique=True)

    def __unicode__(self):
        return self.name

class ExternalFileSource(models.Model):
    name = models.CharField(max_length=50)
    codename = models.SlugField(max_length=50,unique=True)
    service_url = models.URLField(null=True,blank=True)

    def __unicode__(self):
        return self.name

class ExternalFile(models.Model):
    type = models.ForeignKey(ExternalFileType)
    creator = models.ForeignKey(settings.AUTH_USER_MODEL)
    name = models.CharField(max_length=100)
    source_url = models.URLField()
    source_address_info = models.CharField(max_length=200, blank=True)

    def __unicode__(self):
        return self.name
# End External File Models

# Join+ Model for Participants of a competition
class ParticipantStatus(models.Model):
    UNKNOWN = 'unknown'
    DENIED = 'denied'
    APPROVED = 'approved'
    PENDING = 'pending'
    name = models.CharField(max_length=30)
    codename = models.CharField(max_length=30,unique=True)
    description = models.CharField(max_length=50)

    def __unicode__(self):
        return self.name

class Competition(models.Model):
    """ This is the base competition. """
    title = models.CharField(max_length=100)
    description = models.TextField(null=True, blank=True)
    image = models.FileField(upload_to='logos', storage=PublicStorage, null=True, blank=True, verbose_name="Logo")
    image_url_base = models.CharField(max_length=255)
    has_registration = models.BooleanField(default=False, verbose_name="Registration Required")
    start_date = models.DateTimeField(null=True, blank=True, verbose_name="Start Date (UTC)")
    end_date = models.DateTimeField(null=True, blank=True, verbose_name="End Date (UTC)")
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='competitioninfo_creator')
    admins = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name='competition_admins', blank=True, null=True)
    modified_by = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='competitioninfo_modified_by')
    last_modified = models.DateTimeField(auto_now_add=True)
    pagecontainers = generic.GenericRelation(PageContainer)
    published = models.BooleanField(default=False, verbose_name="Publicly Available")
    # Let's assume the first phase never needs "migration"
    last_phase_migration = models.PositiveIntegerField(default=1)
    is_migrating = models.BooleanField(default=False)
    force_submission_to_leaderboard = models.BooleanField(default=False)
    disallow_leaderboard_modifying = models.BooleanField(default=False)
    secret_key = UUIDField(version=4)
    enable_medical_image_viewer = models.BooleanField(default=False)
    enable_detailed_results = models.BooleanField(default=False)
    original_yaml_file = models.TextField(default='', blank=True, null=True)
    show_datasets_from_yaml = models.BooleanField(default=True, blank=True)
    reward = models.PositiveIntegerField(null=True, blank=True)
    is_migrating_delayed = models.BooleanField(default=False)
    allow_teams = models.BooleanField(default=False)
    enable_per_submission_metadata = models.BooleanField(default=False)
    allow_public_submissions = models.BooleanField(default=False, verbose_name="Allow sharing of public submissions")
    enable_forum = models.BooleanField(default=True)
    enable_teams = models.BooleanField(default=True)
    require_team_approval = models.BooleanField(default=True)
    anonymous_leaderboard = models.BooleanField(default=False)

    teams = models.ManyToManyField(Team, related_name='competition_teams', blank=True, null=True)

    @property
    def pagecontent(self):
        items = list(self.pagecontainers.all())
        return items[0] if len(items) > 0 else None

    def get_absolute_url(self):
        return reverse('competitions:view', kwargs={'pk':self.pk})

    class Meta:
        permissions = (
            ('is_owner', 'Owner'),
            ('can_edit', 'Edit'),
            )
        ordering = ['end_date']

    def __unicode__(self):
        return self.title

    def set_owner(self,user):
        return assign_perm('view_task', user, self)

    def save(self, *args, **kwargs):
        # Make sure the image_url_base is set from the actual storage implementation
        self.image_url_base = self.image.storage.url('')

        phases = self.phases.all().order_by('start_date')
        if len(phases) > 0:
            self.start_date = phases[0].start_date.replace(tzinfo=None)

        # Do the real save
        # cache bust TODO.
        # cache.set("c(id)_one at a time", None, 30)
        return super(Competition,self).save(*args,**kwargs)

    @cached_property
    def image_url(self):
        # Return the transformed image_url
        if self.image:
            return os.path.join(self.image_url_base, self.image.name)
        return None

    @cached_property
    def get_start_date(self):
        if not self.start_date:
            # Save sets the start date, so let's set it!
            self.save()
        return self.start_date

    @property
    def is_active(self):
        if self.end_date is None:
            return True
        if type(self.end_date) is datetime.datetime.date:
            return True if self.end_date is None else self.end_date > now().date()
        if type(self.end_date) is datetime.datetime:
            return True if self.end_date is None else self.end_date > now()

    def do_phase_migration(self, current_phase, last_phase):
        '''
        Does the actual migrating of submissions from last_phase to current_phase

        current_phase: The new phase object we are entering
        last_phase: The phase object to transfer submissions from
        '''
        logger.info("Checking for submissions that may still be running competition pk=%s" % self.pk)

        if last_phase.submissions.filter(status__codename=CompetitionSubmissionStatus.RUNNING).exists():
            logger.info('Some submissions still marked as processing for competition pk=%s' % self.pk)
            self.is_migrating_delayed = True
            self.save()
            return
        else:
            logger.info("No submissions running for competition pk=%s" % self.pk)

        logger.info('Doing phase migration on competition pk=%s from phase: %s to phase: %s' %
                    (self.pk, last_phase.phasenumber, current_phase.phasenumber))

        if self.is_migrating:
            logger.info('Trying to migrate competition pk=%s, but it is already being migrated!' % self.pk)
            return

        self.is_migrating = True
        self.save()

        try:
            submissions = []
            leader_board = PhaseLeaderBoard.objects.get(phase=last_phase)

            leader_board_entries = PhaseLeaderBoardEntry.objects.filter(board=leader_board)
            for submission in leader_board_entries:
                submissions.append(submission.result)

            participants = {}

            for s in submissions:
                participants[s.participant] = s

            from tasks import evaluate_submission

            for participant, submission in participants.items():
                logger.info('Moving submission %s over' % submission)

                new_submission = CompetitionSubmission(
                    participant=participant,
                    file=submission.file,
                    phase=current_phase
                )
                new_submission.save(ignore_submission_limits=True)

                evaluate_submission(new_submission.pk, current_phase.is_scoring_only)
        except PhaseLeaderBoard.DoesNotExist:
            pass

        current_phase.is_migrated = True
        current_phase.save()

        # TODO: ONLY IF SUCCESSFUL
        self.is_migrating = False # this should really be True until evaluate_submission tasks are all the way completed
        self.is_migrating_delayed = False
        self.last_phase_migration = current_phase.phasenumber
        self.save()

    def check_trailing_phase_submissions(self):
        '''
        Checks that the requested competition has all submissions in the current phase, none trailing in the previous
        phase
        '''
        if self.is_migrating:
            logger.info('Trying to check migrations on competition pk=%s, but it is already being migrated!' % self.pk)
            return

        last_phase = None
        current_phase = None

        for phase in self.phases.all():
            if phase.is_active:
                current_phase = phase
                break

            last_phase = phase

        if current_phase is None or last_phase is None:
            return

        logger.info("Checking for needed migrations on competition pk=%s, last phase: %s, current phase: %s" %
                    (self.pk, last_phase.phasenumber, current_phase.phasenumber))

        if current_phase.phasenumber > self.last_phase_migration:
            if current_phase.auto_migration:
                self.do_phase_migration(current_phase, last_phase)

    def check_future_phase_sumbmissions(self):
        '''
        Checks for if we need to migrate current phase submissions to next phase
        1. First will check if competition phase is being migrated
        2. We need to keep track of current phase and next phase
        3. Iteration:
            1. If phase is active, it becomes our current phase
            2. If current phasenumbers is less than last phase, then we know there is a next phase
        4. Break the foor loop to proceed
        5. Check if either current_phase or next_phase is None, if true just return
        6. Just log some info
        '''

        if self.is_migrating:
            logger.info("Checking for migrations on competition pk=%s, but it is already being migrated" % self.pk)
            return

        current_phase = None
        next_phase = None

        phases = self.phases.all()
        if len(phases) == 0:
            return

        last_phase = phases.reverse()[0]

        for index, phase in enumerate(phases):
            if phase.is_active:
                current_phase = phase
                if current_phase.phasenumber < last_phase.phasenumber:
                    next_phase = phases[index + 1]
                break

        if current_phase is None or next_phase is None:
            return

        logger.info("Checking for needed migrations on competition pk=%s, current phase: %s, next phase: %s" %
                    (self.pk, current_phase.phasenumber, next_phase.phasenumber))

        if next_phase.phasenumber > self.last_phase_migration:
            if next_phase.auto_migration:
                self.apply_phase_migration(current_phase, next_phase)

    def apply_phase_migration(self, current_phase, next_phase):
        '''
        Does the actual migrating of submissions from last_phase to current_phase

        current_phase: The new phase object we are entering
        last_phase: The phase object to transfer submissions from
        '''
        logger.info("Checking for submissions that may still be running competition pk=%s" % self.pk)

        if current_phase.submissions.filter(status__codename=CompetitionSubmissionStatus.RUNNING).exists():
            logger.info('Some submissions still marked as processing for competition pk=%s' % self.pk)
            self.is_migrating_delayed = True
            self.save()
            return
        else:
            logger.info("No submissions running for competition pk=%s" % self.pk)

        logger.info('Doing phase migration on competition pk=%s from phase: %s to phase: %s' %
                    (self.pk, current_phase.phasenumber, next_phase.phasenumber))

        if self.is_migrating:
            logger.info('Trying to migrate competition pk=%s, but it is already being migrated!' % self.pk)
            return

        self.is_migrating = True
        self.save()

        try:
            submissions = []
            leader_board = PhaseLeaderBoard.objects.get(phase=current_phase)

            leader_board_entries = PhaseLeaderBoardEntry.objects.filter(board=leader_board)
            for submission in leader_board_entries:
                submissions.append(submission.result)

            participants = {}

            for s in submissions:
                if s.is_migrated is False:
                    participants[s.participant] = s

            from tasks import evaluate_submission

            for participant, submission in participants.items():
                logger.info('Moving submission %s over' % submission)

                new_submission = CompetitionSubmission(
                    participant=participant,
                    file=submission.file,
                    phase=next_phase
                )
                new_submission.save(ignore_submission_limits=True)

                submission.is_migrated = True
                submission.save()

                evaluate_submission(new_submission.pk, current_phase.is_scoring_only)
        except PhaseLeaderBoard.DoesNotExist:
            pass

        # To check for submissions being migrated, does not allow to enter new submission
        next_phase.is_migrated = True
        next_phase.save()

        # TODO: ONLY IF SUCCESSFUL
        self.is_migrating = False # this should really be True until evaluate_submission tasks are all the way completed
        self.is_migrating_delayed = False
        self.last_phase_migration = current_phase.phasenumber
        self.save()

    def get_results_csv(self, phase_pk, include_scores_not_on_leaderboard=False):
        phase = self.phases.get(pk=phase_pk)
        if phase.is_blind:
            return 'Not allowed, phase is blind.'

        groups = phase.scores(include_scores_not_on_leaderboard=include_scores_not_on_leaderboard)

        csvfile = StringIO.StringIO()
        csvwriter = csv.writer(csvfile)

        for group in groups:
            #csvwriter.writerow([group['label']])
            #csvwriter.writerow([])

            headers = ["User"]
            sub_headers = [""]
            # This ordering dict will contain {<header key>: <order of the column>}
            ordering = {}

            for count, header in enumerate(group['headers']):
                ordering[header['key']] = count
                subs = header['subs']
                if subs:
                    for sub in subs:
                        headers.append(header['label'])
                        sub_headers.append(sub['label'])
                else:
                    headers.append(header['label'])
            csvwriter.writerow(['submission_pk',] + headers)
            if sub_headers != ['']:
                csvwriter.writerow(sub_headers)

            try:
                if len(group['scores']) <= 0:
                    csvwriter.writerow(["No data available"])
                else:
                    for pk, scores in group['scores']:
                        #print pk, scores
                        row = [scores['username']] + (['']*(len(ordering) + 1))
                        for v in scores['values']:
                            if 'rnk' in v:
                                # Based on the header label insert the score into the proper column
                                row[ordering[v['name']] + 1] = "%s (%s)" % (v['val'], v['rnk'])
                            else:
                                row[ordering[v['name']] + 1] = "%s (%s)" % (v['val'], v['hidden_rnk'])
                        csvwriter.writerow([scores['id'],] + row)
            except:
                csvwriter.writerow(["Exception parsing scores!"])
                logger.error("Error parsing scores for competition PK=%s" % self.pk)

            csvwriter.writerow([])
            csvwriter.writerow([])

        return csvfile.getvalue()

    def get_score_headers(self):
        qs = self.submissionscoredef_set.filter(computed=False)
        qs = qs.order_by('ordering').values_list('label', flat=True)
        return qs

    @cached_property
    def get_participant_count(self):
        return self.participants.all().count()


post_save.connect(Forum.competition_post_save, sender=Competition)


class Page(models.Model):
    category = TreeForeignKey(ContentCategory)
    defaults = models.ForeignKey(DefaultContentItem, null=True, blank=True)
    codename = models.SlugField(max_length=100)
    container = models.ForeignKey(PageContainer, related_name='pages', verbose_name="Competition")
    title = models.CharField(max_length=100, null=True, blank=True)
    label = models.CharField(max_length=100, verbose_name="Title")
    rank = models.IntegerField(default=0, verbose_name="Order")
    visibility = models.BooleanField(default=True, verbose_name="Visible")
    markup = models.TextField(blank=True)
    html = models.TextField(blank=True, verbose_name="Content")
    competition = models.ForeignKey(Competition, related_name='pages', null=True)

    def __unicode__(self):
        return self.title

    class Meta:
        unique_together = (('label','category','container'),)
        ordering = ['category', 'rank']

    def save(self,*args,**kwargs):
        #if not self.title:
        #    self.title = "%s - %s" % ( self.container.name, self.label )
        if self.defaults:
            if self.category != self.defaults.category:
                raise Exception("Defaults category must match Item category")
            if self.defaults.required and self.visibility is False:
                raise Exception("Item is required and must be visible")
        return super(Page,self).save(*args,**kwargs)

# Dataset model
class Dataset(models.Model):
    """
        This is a dataset for a competition.
        TODO: Consider if this is replaced or reimplemented as a 'bundle of data'.
    """
    creator =  models.ForeignKey(settings.AUTH_USER_MODEL, related_name='datasets')
    name = models.CharField(max_length=50)
    description = models.TextField()
    number = models.PositiveIntegerField(default=1)
    datafile = models.ForeignKey(ExternalFile)

    class Meta:
        ordering = ["number"]

    def __unicode__(self):
        return "%s [%s]" % (self.name,self.datafile.name)

def competition_prefix(competition):
    return os.path.join("competition", str(competition.id))

def phase_prefix(phase):
    return os.path.join(competition_prefix(phase.competition), str(phase.phasenumber))

def phase_data_prefix(phase):
    return os.path.join("competition", str(phase.competition.id), str(phase.phasenumber), "data")

# In the following helpers, the filename argument is required even though we
# choose to not use it. See FileField.upload_to at https://docs.djangoproject.com/en/dev/ref/models/fields/.

def phase_scoring_program_file(phase, filename="program.zip"):
    return os.path.join(phase_data_prefix(phase), filename)

def phase_reference_data_file(phase, filename="reference.zip"):
    return os.path.join(phase_data_prefix(phase), filename)

def phase_input_data_file(phase, filename="input.zip"):
    return os.path.join(phase_data_prefix(phase), filename)

def submission_root(instance):
    # will generate /competition/1/1/submissions/1/1/
    return os.path.join(
        "competition",
        str(instance.phase.competition.pk),
        str(instance.phase.pk),
        "submissions",
        str(instance.participant.user.pk),
        str(instance.pk),
    )

def submission_file_name(instance, filename="predictions.zip"):
    return os.path.join(submission_root(instance), filename)

def submission_inputfile_name(instance, filename="input.txt"):
    return os.path.join(submission_root(instance), filename)

def submission_history_file_name(instance, filename="history.txt"):
    return os.path.join(submission_root(instance), filename)

def submission_scores_file_name(instance, filename="scores.txt"):
    return os.path.join(submission_root(instance), filename)

def submission_coopetition_file_name(instance, filename="coopetition.zip"):
    return os.path.join(submission_root(instance), filename)

def submission_runfile_name(instance, filename="run.txt"):
    return os.path.join(submission_root(instance), filename)

def submission_detailed_results_filename(instance, filename="detailed_results.html"):
    return os.path.join(submission_root(instance), "run", "html", filename)

def submission_output_filename(instance, filename="output.zip"):
    return os.path.join(submission_root(instance), "run", filename)

def submission_private_output_filename(instance, filename="private_output.zip"):
    return os.path.join(submission_root(instance), "run", filename)

def submission_stdout_filename(instance, filename="stdout.txt"):
    return os.path.join(submission_root(instance), "run", filename)

def submission_stderr_filename(instance, filename="stderr.txt"):
    return os.path.join(submission_root(instance), "run", filename)

def predict_submission_stdout_filename(instance, filename="prediction_stdout_file.txt"):
    return os.path.join(submission_root(instance), "pred", "run", filename)

def predict_submission_stderr_filename(instance, filename="prediction_stderr_file.txt"):
    return os.path.join(submission_root(instance), "pred", "run", filename)

def submission_prediction_runfile_name(instance, filename="run.txt"):
    return os.path.join(submission_root(instance), "pred", filename)

def submission_prediction_output_filename(instance, filename="output.zip"):
    return os.path.join(submission_root(instance), "pred", "run", filename)

class _LeaderboardManagementMode(object):
    """
    Provides a set of constants which define when results become visible to participants
    and how successful submmisions are added to the leaderboard.
    """
    @property
    def DEFAULT(self):
        """
        Specifies that results are visible as soon as they are available and that adding
        a successful submission to the leaderboard is a manual step.
        """
        return 'default'
    @property
    def HIDE_RESULTS(self):
        """
        Specifies that results are hidden from participants until competition owners make
        them visible and that a participant's last successful submission is automatically
        added to the leaderboard.
        """
        return 'hide_results'
    def is_valid(self, mode):
        """Returns true if the given string is a valid constant to define a management mode."""
        return mode == self.DEFAULT or mode == self.HIDE_RESULTS

LeaderboardManagementMode = _LeaderboardManagementMode()

# Competition Phase
class CompetitionPhase(models.Model):
    """
        A phase of a competition.
    """
    COLOR_CHOICES = (
        ('white', 'White'),
        ('orange', 'Orange'),
        ('yellow', 'Yellow'),
        ('green', 'Green'),
        ('blue', 'Blue'),
        ('purple', 'Purple'),
    )

    competition = models.ForeignKey(Competition,related_name='phases')
    description = models.CharField(max_length=1000, null=True, blank=True)
    # Is this 0 based or 1 based?
    phasenumber = models.PositiveIntegerField(verbose_name="Number")
    label = models.CharField(max_length=50, blank=True, verbose_name="Name")
    start_date = models.DateTimeField(verbose_name="Start Date (UTC)")
    max_submissions = models.PositiveIntegerField(default=100, verbose_name="Maximum Submissions (per User)")
    max_submissions_per_day = models.PositiveIntegerField(default=999, verbose_name="Max Submissions (per User) per day")
    is_scoring_only = models.BooleanField(default=True, verbose_name="Results Scoring Only")
    scoring_program = models.FileField(upload_to=phase_scoring_program_file, storage=BundleStorage,null=True,blank=True, verbose_name="Scoring Program")
    reference_data = models.FileField(upload_to=phase_reference_data_file, storage=BundleStorage,null=True,blank=True, verbose_name="Reference Data")
    input_data = models.FileField(upload_to=phase_input_data_file, storage=BundleStorage,null=True,blank=True, verbose_name="Input Data")
    datasets = models.ManyToManyField(Dataset, blank=True, related_name='phase')
    leaderboard_management_mode = models.CharField(max_length=50, default=LeaderboardManagementMode.DEFAULT, verbose_name="Leaderboard Mode")
    auto_migration = models.BooleanField(default=False)
    is_migrated = models.BooleanField(default=False)
    execution_time_limit = models.PositiveIntegerField(default=(5 * 60), verbose_name="Execution time limit (in seconds)")
    color = models.CharField(max_length=24, choices=COLOR_CHOICES, blank=True, null=True)

    input_data_organizer_dataset = models.ForeignKey('OrganizerDataSet', null=True, blank=True, related_name="input_data_organizer_dataset", verbose_name="Input Data", on_delete=models.SET_NULL)
    reference_data_organizer_dataset = models.ForeignKey('OrganizerDataSet', null=True, blank=True, related_name="reference_data_organizer_dataset", verbose_name="Reference Data", on_delete=models.SET_NULL)
    scoring_program_organizer_dataset = models.ForeignKey('OrganizerDataSet', null=True, blank=True, related_name="scoring_program_organizer_dataset", verbose_name="Scoring Program", on_delete=models.SET_NULL)
    phase_never_ends = models.BooleanField(default=False)

    class Meta:
        ordering = ['phasenumber']

    def __unicode__(self):
        return "%s - %s" % (self.competition.title, self.phasenumber)

    @property
    def is_active(self):
        """ Returns true when this phase of the competition is on-going. """
        if self.phase_never_ends:
            return True
        else:
            next_phase = self.competition.phases.filter(phasenumber=self.phasenumber+1)
            if (next_phase is not None) and (len(next_phase) > 0):
                # there is a phase following this phase, thus this phase is active if the current date
                # is between the start of this phase and the start of the next phase
                return self.start_date <= now() and (now() < next_phase[0].start_date)
            else:
                # there is no phase following this phase, thus this phase is active if the current data
                # is after the start date of this phase and the competition is "active"
                return self.start_date <= now() and self.competition.is_active

    @property
    def is_future(self):
        """ Returns true if this phase of the competition has yet to start. """
        return now() < self.start_date

    @property
    def is_past(self):
        """ Returns true if this phase of the competition has already ended. """
        return (not self.is_active) and (not self.is_future)

    @property
    def is_blind(self):
        """
        Indicates whether results are always hidden from participants.
        """
        return self.leaderboard_management_mode == LeaderboardManagementMode.HIDE_RESULTS

    @staticmethod
    def rank_values(ids, id_value_pairs, sort_ascending=True, eps=1.0e-12):
        """ Given a set of identifiers (ids) and a set of (id, value)-pairs
            computes a ranking based on the value. The ranking is provided
            as a set of (id, rank) pairs for all id in ids.
        """
        ranks = {}
        # Only keep pairs for which the key is in the list of ids
        valid_pairs = {k: v for k, v in id_value_pairs.iteritems() if k in ids}
        if len(valid_pairs) == 0:
            return {id: 1 for id in ids}
        # Sort and compute ranks
        sorted_pairs = sorted(valid_pairs.iteritems(), key = operator.itemgetter(1), reverse=not sort_ascending)
        r = 1
        k, v = sorted_pairs[0]
        ranks[k] = r
        for i in range(1, len(sorted_pairs)):
            k, vnow = sorted_pairs[i]
            # Increment the rank only when values are different
            if abs(vnow - v) > eps:
                r = r + 1
                v = vnow
            ranks[k] = r
        # Fill in ranks for ids which were not seen in the input
        r = r + 1
        for id in ids:
            if id not in ranks:
                ranks[id] = r
        return ranks

    @staticmethod
    def rank_submissions(ranks_by_id):
        def compare_ranks(a, b):
            limit = 1000000
            try:
                ia = int(ranks_by_id[a])
            except exceptions.ValueError:
                ia = limit
            try:
                ib = int(ranks_by_id[b])
            except exceptions.ValueError:
                ib = limit
            return ia - ib
        return compare_ranks

    @staticmethod
    def format_value(v, precision="2"):
        p = 1
        try:
            if precision is not None:
                p = min(10, max(1, int(precision)))
        except exceptions.ValueError:
            pass
        return ("{:." + str(p) + "f}").format(v)

    def scores(self, include_scores_not_on_leaderboard=False, **kwargs):
        score_filters = kwargs.pop('score_filters',{})

        # Get the list of submissions in this leaderboard
        submissions = []
        result_location = []
        lb, created = PhaseLeaderBoard.objects.get_or_create(phase=self)
        if not created:
            if include_scores_not_on_leaderboard:
                qs = CompetitionSubmission.objects.filter(
                    phase=self,
                    status__codename=CompetitionSubmissionStatus.FINISHED
                )
                for submission in qs:
                    result_location.append(submission.file.name)
                    submissions.append((submission.pk, submission.participant.user, submission.team))
            else:
                qs = PhaseLeaderBoardEntry.objects.filter(board=lb)
                for entry in qs:
                    result_location.append(entry.result.file.name)

                for entry in qs:
                    submissions.append((entry.result.id, entry.result.participant.user, entry.result.team))

        results = []
        for count, g in enumerate(SubmissionResultGroup.objects.filter(phases__in=[self]).order_by('ordering')):
            label = g.label
            headers = []
            scores = {}

            # add the location of the results on the blob storage to the scores
            for (pk, user, team) in submissions:
                if self.competition.enable_teams:
                    team_name=''
                    if team is not None:
                        team_name=team.name
                else:
                    team_name=user.team_name
                scores[pk] = {
                    'username': user.username,
                    'user_pk': user.pk,
                    'team_name': team_name,
                    'id': pk,
                    'values': [],
                    'resultLocation': result_location[count]
                }

            scoreDefs = []
            columnKeys = {} # maps a column key to its index in headers list
            for x in SubmissionScoreSet.objects.order_by('tree_id','lft').filter(scoredef__isnull=False,
                                                                        scoredef__groups__in=[g],
                                                                        **kwargs).select_related('scoredef', 'parent'):
                if x.parent is not None:
                    columnKey = x.parent.key
                    columnLabel = x.parent.label
                    columnOrdering = x.parent.ordering
                    columnSubLabels = [{'key': x.key, 'label': x.label, 'ordering': x.ordering}]
                else:
                    columnKey = x.key
                    columnLabel = x.label
                    columnOrdering = x.ordering
                    columnSubLabels = []
                if columnKey not in columnKeys:
                    columnKeys[columnKey] = len(headers)
                    headers.append({'key': columnKey, 'label': columnLabel, 'subs': columnSubLabels, 'ordering': columnOrdering})
                else:
                    headers[columnKeys[columnKey]]['subs'].extend(columnSubLabels)

                scoreDefs.append(x.scoredef)
            # Sort headers appropiately
            def sortkey(x):
                return x['ordering']
            headers.sort(key=sortkey, reverse=False)
            for header in headers:
                header['subs'].sort(key=sortkey, reverse=False)
            # compute total column span
            column_span = 2
            for gHeader in headers:
                n = len(gHeader['subs'])
                column_span += n if n > 0 else 1
            # determine which column to select by default
            selection_key, selection_order = None, 0
            for i in range(len(scoreDefs)):
                if (selection_key is None) or (scoreDefs[i].selection_default > selection_order):
                    selection_key, selection_order = scoreDefs[i].key, scoreDefs[i].selection_default

            results.append({ 'label': label, 'headers': headers, 'total_span' : column_span, 'selection_key': selection_key,
                             'scores': scores, 'scoredefs': scoreDefs })

        if len(submissions) > 0:
            # Figure out which submission scores we need to read from the database.
            submission_ids = [id for (id,name) in submissions]
            # not_computed_scoredefs: map (scoredef.id, scoredef) to keep track of non-computed scoredefs
            not_computed_scoredefs = {}
            computed_scoredef_ids = []
            # computed_deps: maps id of a computed scoredef to a list of ids for scoredefs which are
            #                input to the computation
            computed_deps = {}
            for result in results:
                for sdef in result['scoredefs']:
                    if sdef.computed is True:
                        computed_scoredef_ids.append(sdef.id)
                    else:
                        not_computed_scoredefs[sdef.id] = sdef
                if len(computed_scoredef_ids) > 0:
                    computed_ids = SubmissionComputedScore.objects.filter(scoredef_id__in=computed_scoredef_ids).values_list('id')
                    fields = SubmissionComputedScoreField.objects.filter(computed_id__in=computed_ids).select_related('scoredef', 'computed')
                    for field in fields:
                        if not field.scoredef.computed:
                            not_computed_scoredefs[field.scoredef.id] = field.scoredef
                        if field.computed.scoredef_id not in computed_deps:
                            computed_deps[field.computed.scoredef_id] = []
                        computed_deps[field.computed.scoredef_id].append(field.scoredef)
            # Now read the submission scores
            values = {}
            scoredef_ids = [sdef_id for (sdef_id, sdef) in not_computed_scoredefs.iteritems()]
            for s in SubmissionScore.objects.filter(scoredef_id__in=scoredef_ids, result_id__in=submission_ids):
                if s.scoredef_id not in values:
                    values[s.scoredef_id] = {}
                values[s.scoredef_id][s.result_id] = s.value

            # rank values per scoredef.key (not computed)
            ranks = {}
            for (sdef_id, v) in values.iteritems():
                sdef = not_computed_scoredefs[sdef_id]
                ranks[sdef_id] = self.rank_values(submission_ids, v, sort_ascending=sdef.sorting=='asc')

            # compute values for computed scoredefs
            for result in results:
                for sdef in result['scoredefs']:
                    if sdef.computed:
                        operation = getattr(models, sdef.computed_score.operation)
                        if (operation.name == 'Avg'):
                            cnt = len(computed_deps[sdef.id])
                            if (cnt > 0):
                                computed_values = {}
                                for id in submission_ids:
                                    computed_values[id] = sum([ranks[d.id][id] for d in computed_deps[sdef.id]]) / float(cnt)
                                values[sdef.id] = computed_values
                                ranks[sdef.id] = self.rank_values(submission_ids, computed_values, sort_ascending=sdef.sorting=='asc')

            #format values
            for result in results:
                scores = result['scores']
                for sdef in result['scoredefs']:
                    knownValues = {}
                    if sdef.id in values:
                        knownValues = values[sdef.id]
                    knownRanks = {}
                    if sdef.id in ranks:
                        knownRanks = ranks[sdef.id]
                    for id in submission_ids:
                        v = "-"
                        if id in knownValues:
                            v = CompetitionPhase.format_value(knownValues[id], sdef.numeric_format)
                        r = "-"
                        if id in knownRanks:
                            r = knownRanks[id]
                        if sdef.show_rank:
                            scores[id]['values'].append({'val': v, 'rnk': r, 'name' : sdef.key})
                        else:
                            scores[id]['values'].append({'val': v, 'hidden_rnk': r, 'name' : sdef.key})
                    if (sdef.key == result['selection_key']):
                        overall_ranks = ranks[sdef.id]
                ranked_submissions = sorted(submission_ids, cmp=CompetitionPhase.rank_submissions(overall_ranks))
                final_scores = [(overall_ranks[id], scores[id]) for id in ranked_submissions]
                result['scores'] = final_scores
                del result['scoredefs']
        return results

# Competition Participant
class CompetitionParticipant(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL,related_name='participation')
    competition = models.ForeignKey(Competition,related_name='participants')
    status = models.ForeignKey(ParticipantStatus)
    reason = models.CharField(max_length=100,null=True,blank=True)

    class Meta:
        unique_together = (('user','competition'),)

    def __unicode__(self):
        return "%s - %s" % (self.competition.title, self.user.username)

    @property
    def is_approved(self):
        """ Returns true if this participant is approved into the competition. """
        return self.status.codename == ParticipantStatus.APPROVED

# Competition Submission Status
class CompetitionSubmissionStatus(models.Model):
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    RUNNING = "running"
    FAILED = "failed"
    CANCELLED = "cancelled"
    FINISHED = "finished"

    name = models.CharField(max_length=20)
    codename = models.SlugField(max_length=20,unique=True)

    def __unicode__(self):
        return self.name

# Competition Submission
class CompetitionSubmission(models.Model):
    """
    Represents a submission from a competition participant.
    """
    participant = models.ForeignKey(CompetitionParticipant, related_name='submissions')
    phase = models.ForeignKey(CompetitionPhase, related_name='submissions')
    file = models.FileField(upload_to=submission_file_name, storage=BundleStorage, null=True, blank=True)
    file_url_base = models.CharField(max_length=2000, blank=True)
    readable_filename = models.TextField(null=True, blank=True)
    description = models.CharField(max_length=256, blank=True)
    inputfile = models.FileField(upload_to=submission_inputfile_name, storage=BundleStorage, null=True, blank=True)
    runfile = models.FileField(upload_to=submission_runfile_name, storage=BundleStorage, null=True, blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    execution_key = models.TextField(blank=True, default="")
    status = models.ForeignKey(CompetitionSubmissionStatus)
    status_details = models.CharField(max_length=100, null=True, blank=True)
    submission_number = models.PositiveIntegerField(default=0)
    output_file = models.FileField(upload_to=submission_output_filename, storage=BundleStorage, null=True, blank=True)
    private_output_file = models.FileField(upload_to=submission_private_output_filename, storage=BundleStorage, null=True, blank=True)
    stdout_file = models.FileField(upload_to=submission_stdout_filename, storage=BundleStorage, null=True, blank=True)
    stderr_file = models.FileField(upload_to=submission_stderr_filename, storage=BundleStorage, null=True, blank=True)
    history_file = models.FileField(upload_to=submission_history_file_name, storage=BundleStorage, null=True, blank=True)
    scores_file = models.FileField(upload_to=submission_scores_file_name, storage=BundleStorage, null=True, blank=True)
    coopetition_file = models.FileField(upload_to=submission_coopetition_file_name, storage=BundleStorage, null=True, blank=True)
    detailed_results_file = models.FileField(upload_to=submission_detailed_results_filename, storage=BundleStorage, null=True, blank=True)
    prediction_runfile = models.FileField(upload_to=submission_prediction_runfile_name,
                                          storage=BundleStorage, null=True, blank=True)
    prediction_output_file = models.FileField(upload_to=submission_prediction_output_filename,
                                              storage=BundleStorage, null=True, blank=True)
    exception_details = models.TextField(blank=True, null=True)
    prediction_stdout_file = models.FileField(upload_to=predict_submission_stdout_filename, storage=BundleStorage, null=True, blank=True)
    prediction_stderr_file = models.FileField(upload_to=predict_submission_stderr_filename, storage=BundleStorage, null=True, blank=True)

    method_name = models.CharField(max_length=20, null=True, blank=True)
    method_description = models.TextField(null=True, blank=True)
    project_url = models.URLField(null=True, blank=True)
    publication_url = models.URLField(null=True, blank=True)
    bibtex = models.TextField(null=True, blank=True)
    organization_or_affiliation = models.CharField(max_length=255, null=True, blank=True)
    team_name = models.CharField(max_length=64, null=True, blank=True)

    is_public = models.BooleanField(default=False)
    when_made_public = models.DateTimeField(null=True, blank=True)
    when_unmade_public = models.DateTimeField(null=True, blank=True)

    download_count = models.IntegerField(default=0)

    like_count = models.IntegerField(default=0)
    dislike_count = models.IntegerField(default=0)

    is_migrated = models.BooleanField(default=False) # Will be used to auto  migrate

    team = models.ForeignKey(Team, related_name='team', null=True, blank=True)

    class Meta:
        unique_together = (('submission_number','phase','participant'),)

    def __unicode__(self):
        return "%s %s %s %s" % (self.pk, self.phase.competition.title, self.phase.label, self.participant.user.email)

    @property
    def metadata_predict(self):
        '''Generated from the prediction step (if applicable) of evaluation a submission, sometimes competition
        phases are "result only" submissions'''
        return self.metadatas.get(is_predict=True)

    @property
    def metadata_scoring(self):
        '''Generated from the result scoring step of evaluation a submission'''
        return self.metadatas.get(is_scoring=True)

    def save(self, ignore_submission_limits=False, *args, **kwargs):
        print "Saving competition submission."
        if self.participant.competition != self.phase.competition:
            raise Exception("Competition for phase and participant must be the same")

        if self.is_public and not self.when_made_public:
            self.when_made_public = datetime.datetime.utcnow()
        if not self.is_public and self.when_made_public:
            self.when_unmade_public = datetime.datetime.utcnow()

        if hasattr(self, 'status'):
            if self.status.codename == CompetitionSubmissionStatus.RUNNING:
                self.started_at = datetime.datetime.utcnow()
            if self.status.codename == CompetitionSubmissionStatus.FINISHED:
                self.completed_at = datetime.datetime.utcnow()

        self.like_count = self.likes.all().count()
        self.dislike_count = self.dislikes.all().count()

        if not self.readable_filename:
            if hasattr(self, 'file'):
                if self.file.name:
                    try:
                        self.readable_filename = self.file.storage.properties(self.file.name)['x-ms-meta-name']
                    except:
                        self.readable_filename = split(self.file.name)[1]

        # only at save on object creation should it be submitted
        if not self.pk:
            if not ignore_submission_limits:
                print "This is a new submission, getting the submission number."
                subnum = CompetitionSubmission.objects.filter(phase=self.phase, participant=self.participant).aggregate(Max('submission_number'))['submission_number__max']
                if subnum is not None:
                    self.submission_number = subnum + 1
                else:
                    self.submission_number = 1

                failed_count = CompetitionSubmission.objects.filter(phase=self.phase,
                                                                    participant=self.participant,
                                                                    status__name=CompetitionSubmissionStatus.FAILED).count()

                print "This is submission number %d, and %d submissions have failed" % (self.submission_number, failed_count)
                offset_submission_count = self.submission_number - failed_count

                if (offset_submission_count > self.phase.max_submissions):
                    print "Checking to see if the offset_submission_count (%d) is greater than the maximum allowed (%d)" % (offset_submission_count, self.phase.max_submissions)
                    raise PermissionDenied("The maximum number of submissions has been reached.")
                else:
                    print "Submission number below maximum."

                if hasattr(self.phase, 'max_submissions_per_day'):
                    print 'Checking submissions per day count'

                    submissions_from_today_count = len(CompetitionSubmission.objects.filter(
                        phase__competition=self.phase.competition,
                        participant=self.participant,
                        phase=self.phase,
                        submitted_at__gte=datetime.date.today()
                    ))

                    print 'Count is %s and maximum is %s' % (submissions_from_today_count, self.phase.max_submissions_per_day)

                    if submissions_from_today_count + 1 - failed_count > self.phase.max_submissions_per_day or self.phase.max_submissions_per_day == 0:
                        print 'PERMISSION DENIED'
                        raise PermissionDenied("The maximum number of submissions this day have been reached.")
            else:
                # Make sure we're incrementing the number if we're forcing in a new entry
                while CompetitionSubmission.objects.filter(
                    phase=self.phase,
                    participant=self.participant,
                    submission_number=self.submission_number
                ).exists():
                    self.submission_number += 1

            self.status = CompetitionSubmissionStatus.objects.get_or_create(codename=CompetitionSubmissionStatus.SUBMITTING)[0]

        print "Setting the file url base."
        self.file_url_base = self.file.storage.url('')

        # Add current participant team if the competition hallow teams
        if self.participant.competition.enable_teams:
            self.team=get_user_team(self.participant, self.participant.competition)


        print "Calling super save."
        # TODO REMOVE AFTER TESTING
        # c_key = "c%s_public_submissions" % self.phase.competition.id
        # cache.set(c_key, None, 30)  # cache busting for CompetitionDetailView
        res = super(CompetitionSubmission,self).save(*args,**kwargs)
        return res

    def get_filename(self):
        """
        Returns the short name of the file which was uploaded to create the submission.
        """
        if not self.readable_filename:
            self.save()
        return self.readable_filename

    def get_file_for_download(self, key, requested_by):
        """
        Returns the FileField object for the file that is to be downloaded by the given user.

        key: A name identifying the file to download. The choices are 'input.zip', 'output.zip',
           'prediction-output.zip', 'stdout.txt', 'stderr.txt', 'history.txt' or 'private_output.zip'
        requested_by: A user object identifying the user making the request to access the file.

        Raises:
           ValueError exception for improper arguments.
           PermissionDenied exception when access to the file cannot be granted.
        """
        downloadable_files = {
            'input.zip': ('file', 'zip', False),
            'output.zip': ('output_file', 'zip', True),
            'private_output.zip': ('private_output_file', 'zip', True),
            'prediction-output.zip': ('prediction_output_file', 'zip', True),
            'stdout.txt': ('stdout_file', 'txt', True),
            'stderr.txt': ('stderr_file', 'txt', False),
            'predict_stdout.txt': ('prediction_stdout_file', 'txt', True),
            'predict_stderr.txt': ('prediction_stderr_file', 'txt', True),
            'detailed_results.html': ('detailed_results_file', 'html', True),
        }
        if key not in downloadable_files:
            raise ValueError("File requested is not valid.")
        file_attr, file_ext, file_has_restricted_access = downloadable_files[key]

        competition = self.phase.competition
        if competition.creator == requested_by or requested_by in competition.admins.all():
            pass
        elif not self.is_public:
            # If the user requesting access is the owner, access granted
            if self.participant.competition.creator.id != requested_by.id:
                # User making request must be owner of this submission and be granted
                # download privilege by the competition owners.
                if self.participant.user.id != requested_by.id:
                    raise PermissionDenied()
                if file_has_restricted_access and self.phase.is_blind:
                    raise PermissionDenied()

        if key == 'private_output.zip':
            if self.participant.competition.creator.id != requested_by.id:
                raise PermissionDenied()

        if key == 'input.zip':
            DownloadRecord.objects.get_or_create(user=requested_by, submission=self)

        if file_ext == 'txt':
            file_type = 'text/plain'
        elif file_ext == 'html':
            file_type = 'text/html'
        else:
            file_type = 'application/zip'
        file_name = "{0}-{1}-{2}".format(self.participant.user.username, self.submission_number, key)
        return getattr(self, file_attr), file_type, file_name

    def get_overall_like_count(self):
        return self.like_count - self.dislike_count

    def get_default_score(self):
        score = self.scores.filter(scoredef__ordering=1)
        if score:
            return score[0].value
        else:
            return None

    def get_scores_as_tuples(self):
        '''Returns a list of score tuples like so:
        [("Score Label", 123), ("Another label", 1.2223)]'''
        scores = []
        for score in self.scores.all().order_by('scoredef__ordering'):
            scores.append((score.scoredef.label, score.value))
        return scores


class SubmissionResultGroup(models.Model):
    competition = models.ForeignKey(Competition)
    key = models.CharField(max_length=50)
    label = models.CharField(max_length=50)
    ordering = models.PositiveIntegerField(default=1)
    phases = models.ManyToManyField(CompetitionPhase,through='SubmissionResultGroupPhase')

    class Meta:
        ordering = ['ordering']

class SubmissionResultGroupPhase(models.Model):
    group = models.ForeignKey(SubmissionResultGroup)
    phase = models.ForeignKey(CompetitionPhase)

    class Meta:
        unique_together = (('group','phase'),)

    def save(self,*args,**kwargs):
        if self.group.competition != self.phase.competition:
            raise IntegrityError("Group and Phase competition must be the same")
        super(SubmissionResultGroupPhase,self).save(*args,**kwargs)

class SubmissionScoreDef(models.Model):
    competition = models.ForeignKey(Competition)
    key = models.SlugField(max_length=50)
    label = models.CharField(max_length=50)
    sorting = models.SlugField(max_length=20,default='asc',choices=(('asc','Ascending'),('desc','Descending')))
    numeric_format = models.CharField(max_length=20,blank=True,null=True)
    show_rank = models.BooleanField(default=False)
    selection_default = models.IntegerField(default=0)
    computed = models.BooleanField(default=False)
    groups = models.ManyToManyField(SubmissionResultGroup,through='SubmissionScoreDefGroup')
    ordering = models.PositiveIntegerField(default=1)

    class Meta:
        unique_together = (('key','competition'),)

    def __unicode__(self):
        return self.label

class CompetitionDefBundle(models.Model):
    config_bundle = models.FileField(upload_to='competition-bundles', storage=BundleStorage)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='owner')
    created_at = models.DateTimeField(auto_now_add=True)

    @staticmethod
    def localize_datetime(dt):
        """
        Returns the given date or datetime as a datetime with tzinfo.
        """
        if type(dt) is str:
            dt = parse_datetime(dt)
        if type(dt) is datetime.date:
            dt = datetime.datetime.combine(dt, datetime.time())
        if not type(dt) is datetime.datetime:
            raise ValueError("Expected a DateTime object but got %s" % dt)
        if dt.tzinfo is None:
            dt = utc.localize(dt)
        return dt

    @transaction.commit_on_success
    def unpack(self):
        """
        This method unpacks a competition bundle and creates a competition from
        the assets found inside (the competition bundle). The format of the
        competition bundle is described on the CodaLab Wiki:
        https://github.com/codalab/codalab/wiki/12.-Building-a-Competition-Bundle
        """
        # Get the bundle data, which is stored as a zipfile
        logger.info("CompetitionDefBundle::unpack begins (pk=%s)", self.pk)
        zf = zipfile.ZipFile(self.config_bundle)
        logger.debug("CompetitionDefBundle::unpack creating base competition (pk=%s)", self.pk)
        comp_spec_file = [x for x in zf.namelist() if ".yaml" in x ][0]
        yaml_contents = zf.open(comp_spec_file).read()

        # Forcing YAML to interpret the file while maintaining the original order things are in
        from collections import OrderedDict
        _mapping_tag = yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG

        def dict_representer(dumper, data):
            return dumper.represent_dict(data.iteritems())

        def dict_constructor(loader, node):
            return OrderedDict(loader.construct_pairs(node))

        yaml.add_representer(OrderedDict, dict_representer)
        yaml.add_constructor(_mapping_tag, dict_constructor)

        comp_spec = yaml.load(yaml_contents)
        comp_base = comp_spec.copy()
        for block in ['html', 'phases', 'leaderboard']:
            if block in comp_base:
                del comp_base[block]
        comp_base['creator'] = self.owner
        comp_base['modified_by'] = self.owner
        comp_base['original_yaml_file'] = yaml_contents

        if 'end_date' in comp_base:
            if comp_base['end_date'] is None:
                del comp_base['end_date']
            else:
                comp_base['end_date'] = CompetitionDefBundle.localize_datetime(comp_base['end_date'])

        admin_names = None
        if 'admin_names' in comp_base:
            admin_names = comp_base['admin_names']
            del comp_base['admin_names']

        comp = Competition(**comp_base)
        comp.save()
        logger.debug("CompetitionDefBundle::unpack created base competition (pk=%s)", self.pk)

        if admin_names:
            logger.debug("CompetitionDefBundle::unpack looking up admins %s", comp_spec['admin_names'])
            admins = User.objects.filter(username__in=admin_names.split(','))
            logger.debug("CompetitionDefBundle::unpack found admins %s", admins)
            comp.admins.add(*admins)

            logger.debug("CompetitionDefBundle::unpack adding admins as participants")
            approved_status = ParticipantStatus.objects.get(codename=ParticipantStatus.APPROVED)

            for admin in admins:
                try:
                    participant = CompetitionParticipant.objects.get(user=admin, competition=comp)
                    participant.status = approved_status
                    participant.save()
                except models.ObjectDoesNotExist:
                    CompetitionParticipant.objects.create(user=admin, competition=comp, status=approved_status)


        # Unpack and save the logo
        if 'image' in comp_base:
            comp.image.save(comp_base['image'], File(io.BytesIO(zf.read(comp_base['image']))))
            comp.save()
            logger.debug("CompetitionDefBundle::unpack saved competition logo (pk=%s)", self.pk)

        # Populate competition pages
        pc,_ = PageContainer.objects.get_or_create(object_id=comp.id, content_type=ContentType.objects.get_for_model(comp))
        details_category = ContentCategory.objects.get(name="Learn the Details")
        Page.objects.create(category=details_category, container=pc,  codename="overview", competition=comp,
                                   label="Overview", rank=0, html=zf.read(comp_spec['html']['overview']))
        Page.objects.create(category=details_category, container=pc,  codename="evaluation", competition=comp,
                                   label="Evaluation", rank=1, html=zf.read(comp_spec['html']['evaluation']))
        Page.objects.create(category=details_category, container=pc,  codename="terms_and_conditions", competition=comp,
                                   label="Terms and Conditions", rank=2, html=zf.read(comp_spec['html']['terms']))

        default_pages = ('overview', 'evaluation', 'terms', 'data')

        for (page_number, (page_name, page_data)) in enumerate(comp_spec['html'].items()):
            if page_name not in default_pages:
                Page.objects.create(
                    category=details_category,
                    container=pc,
                    codename=page_name,
                    competition=comp,
                    label=page_name,
                    rank=3 + page_number,     # Start at 3 (Overview, Evaluation and Terms and Conditions first)
                    html=zf.read(page_data)
                )

        participate_category = ContentCategory.objects.get(name="Participate")
        Page.objects.create(category=participate_category, container=pc,  codename="get_data", competition=comp,
                                   label="Get Data", rank=0, html=zf.read(comp_spec['html']['data']))
        Page.objects.create(category=participate_category, container=pc,  codename="submit_results", label="Submit / View Results", rank=1, html="")
        logger.debug("CompetitionDefBundle::unpack created competition pages (pk=%s)", self.pk)

        data_set_cache = {}

        # Create phases
        for index, p_num in enumerate(comp_spec['phases']):
            phase_spec = comp_spec['phases'][p_num].copy()
            phase_spec['competition'] = comp

            if 'leaderboard_management_mode' in phase_spec:
                if not LeaderboardManagementMode.is_valid(phase_spec['leaderboard_management_mode']):
                    msg = "Invalid leaderboard_management_mode ({0}) specified for phase {1}. Reverting to default."
                    logger.warn(msg.format(phase_spec['leaderboard_management_mode'], p_num))
                    phase_spec['leaderboard_management_mode'] = LeaderboardManagementMode.DEFAULT
            else:
                phase_spec['leaderboard_management_mode'] = LeaderboardManagementMode.DEFAULT

            if 'datasets' in phase_spec:
                datasets = phase_spec['datasets']

                for dataset_index, dataset in datasets.items():
                    if "key" in dataset:
                        dataset["url"] = reverse("datasets_download", kwargs={"dataset_key": dataset["key"]})

                del phase_spec['datasets']
            else:
                datasets = {}

            phase_spec['start_date'] = CompetitionDefBundle.localize_datetime(phase_spec['start_date'])

            # First phase can't have auto_migration=True, remove that here
            if index == 0:
                phase_spec['auto_migration'] = False
                comp.last_phase_migration = phase_spec['phasenumber']
                logger.debug('Set last_phase_migration to #%s' % phase_spec['phasenumber'])
                comp.save()

            phase, created = CompetitionPhase.objects.get_or_create(**phase_spec)
            logger.debug("CompetitionDefBundle::unpack created phase (pk=%s)", self.pk)

            # Set default for max submissions per day
            if not hasattr(phase, 'max_submissions_per_day'):
                phase.max_submissions_per_day = 999

            # Create automatic datasets out of some included files, cache file names here as to not make duplicates
            # where many phases use same dataset files
            if hasattr(phase, 'scoring_program') and phase.scoring_program:
                if phase_spec["scoring_program"].endswith(".zip"):
                    phase.scoring_program.save(phase_scoring_program_file(phase), File(io.BytesIO(zf.read(phase_spec['scoring_program']))))

                    file_name = os.path.splitext(os.path.basename(phase_spec['scoring_program']))[0]
                    if phase_spec['scoring_program'] not in data_set_cache:
                        logger.debug('Adding organizer dataset to cache: %s' % phase_spec['scoring_program'])
                        data_set_cache[phase_spec['scoring_program']] = OrganizerDataSet.objects.create(
                            name="%s_%s_%s" % (file_name, phase.phasenumber, comp.pk),
                            type="Scoring Program",
                            data_file=phase.scoring_program.file.name,
                            uploaded_by=self.owner
                        )
                    phase.scoring_program_organizer_dataset = data_set_cache[phase_spec['scoring_program']]
                else:
                    logger.debug("CompetitionDefBundle::unpack getting dataset for scoring_program with key %s", phase_spec["scoring_program"])
                    try:
                        data_set = OrganizerDataSet.objects.get(key=phase_spec["scoring_program"])
                        phase.scoring_program = data_set.data_file.file.name
                        phase.scoring_program_organizer_dataset = data_set
                    except OrganizerDataSet.DoesNotExist:
                        assert False, "OrganizerDataSet (%s) could not be found" % phase_spec["scoring_program"]

            if hasattr(phase, 'reference_data') and phase.reference_data:
                if phase_spec["reference_data"].endswith(".zip"):
                    phase.reference_data.save(phase_reference_data_file(phase), File(io.BytesIO(zf.read(phase_spec['reference_data']))))

                    file_name = os.path.splitext(os.path.basename(phase_spec['reference_data']))[0]
                    if phase_spec['reference_data'] not in data_set_cache:
                        logger.debug('Adding organizer dataset to cache: %s' % phase_spec['reference_data'])
                        data_set_cache[phase_spec['reference_data']] = OrganizerDataSet.objects.create(
                            name="%s_%s_%s" % (file_name, phase.phasenumber, comp.pk),
                            type="Reference Data",
                            data_file=phase.reference_data.file.name,
                            uploaded_by=self.owner
                        )
                    phase.reference_data_organizer_dataset = data_set_cache[phase_spec['reference_data']]
                else:
                    logger.debug("CompetitionDefBundle::unpack getting dataset for reference_data with key %s", phase_spec["reference_data"])
                    try:
                        data_set = OrganizerDataSet.objects.get(key=phase_spec["reference_data"])
                        phase.reference_data = data_set.data_file.file.name
                        phase.reference_data_organizer_dataset = data_set
                    except OrganizerDataSet.DoesNotExist:
                        assert False, "OrganizerDataSet (%s) could not be found" % phase_spec["reference_data"]

            if 'input_data' in phase_spec:
                if phase_spec["input_data"].endswith(".zip"):
                    phase.input_data.save(phase_input_data_file(phase), File(io.BytesIO(zf.read(phase_spec['input_data']))))

                    file_name = os.path.splitext(os.path.basename(phase_spec['input_data']))[0]
                    if phase_spec['input_data'] not in data_set_cache:
                        logger.debug('Adding organizer dataset to cache: %s' % phase_spec['input_data'])
                        data_set_cache[phase_spec['input_data']] = OrganizerDataSet.objects.create(
                            name="%s_%s_%s" % (file_name, phase.phasenumber, comp.pk),
                            type="Input Data",
                            data_file=phase.input_data.file.name,
                            uploaded_by=self.owner
                        )
                    phase.input_data_organizer_dataset = data_set_cache[phase_spec['input_data']]
                else:
                    logger.debug("CompetitionDefBundle::unpack getting dataset for input_data with key %s", phase_spec["input_data"])
                    try:
                        data_set = OrganizerDataSet.objects.get(key=phase_spec["input_data"])
                        phase.input_data = data_set.data_file.file.name
                        phase.input_data_organizer_dataset = data_set
                    except OrganizerDataSet.DoesNotExist:
                        assert False, "OrganizerDataSet (%s) could not be found" % phase_spec["input_data"]

            phase.auto_migration = bool(phase_spec.get('auto_migration', False))
            phase.save()
            logger.debug("CompetitionDefBundle::unpack saved scoring program and reference data (pk=%s)", self.pk)
            eft,cr_=ExternalFileType.objects.get_or_create(name="Data", codename="data")
            count = 1
            for ds in datasets.keys():
                f = ExternalFile.objects.create(type=eft, source_url=datasets[ds]['url'], name=datasets[ds]['name'], creator=self.owner)
                f.save()
                d = Dataset.objects.create(creator=self.owner, datafile=f, number=count)
                d.save()
                phase.datasets.add(d)
                phase.save()
                count += 1
            logger.debug("CompetitionDefBundle::unpack saved datasets (pk=%s)", self.pk)

        logger.debug("CompetitionDefBundle::unpack saved created competition phases (pk=%s)", self.pk)

        # Create leaderboard
        if 'leaderboard' in comp_spec:
            # If there's more than one create each of them
            if 'leaderboards' in comp_spec['leaderboard']:
                leaderboards = {}
                for key, value in comp_spec['leaderboard']['leaderboards'].items():
                    rg,cr = SubmissionResultGroup.objects.get_or_create(competition=comp, key=value['label'].strip(), label=value['label'].strip(), ordering=value['rank'])
                    leaderboards[rg.label] = rg
                    for gp in comp.phases.all():
                        rgp,crx = SubmissionResultGroupPhase.objects.get_or_create(phase=gp, group=rg)
            logger.debug("CompetitionDefBundle::unpack created leaderboard (pk=%s)", self.pk)

            # Create score groups
            if 'column_groups' in comp_spec['leaderboard']:
                groups = {}
                for key, vals in comp_spec['leaderboard']['column_groups'].items():
                    index = comp_spec['leaderboard']['column_groups'].keys().index(key) + 1
                    if vals is None:
                        vals = dict()
                    setdefaults = {
                        'label' : "" if 'label' not in vals else vals['label'].strip(),
                        'ordering' : index if 'rank' not in vals else vals['rank']
                    }
                    s,cr = SubmissionScoreSet.objects.get_or_create(competition=comp, key=key.strip(), defaults=setdefaults)
                    groups[s.label] = s
            logger.debug("CompetitionDefBundle::unpack created score groups (pk=%s)", self.pk)

            # Create scores.
            if 'columns' in comp_spec['leaderboard']:
                columns = {}
                for key, vals in comp_spec['leaderboard']['columns'].items():
                    index = comp_spec['leaderboard']['columns'].keys().index(key) + 1
                    # Do non-computed columns first
                    if 'computed' in vals:
                        continue
                    sdefaults = {
                                    'label' : "" if 'label' not in vals else vals['label'].strip(),
                                    'numeric_format' : "2" if 'numeric_format' not in vals else vals['numeric_format'],
                                    'show_rank' : True,
                                    'sorting' : 'desc' if 'sort' not in vals else vals['sort'],
                                    'ordering' : index if 'rank' not in vals else vals['rank']
                                    }
                    if 'selection_default' in vals:
                        sdefaults['selection_default'] = vals['selection_default']

                    sd,cr = SubmissionScoreDef.objects.get_or_create(
                                competition=comp,
                                key=key,
                                computed=False,
                                defaults=sdefaults)
                    columns[sd.key] = sd

                    # Associate the score definition with its column group
                    if 'column_group' in vals:
                        gparent = groups[vals['column_group']['label']]
                        g,cr = SubmissionScoreSet.objects.get_or_create(
                                competition=comp,
                                parent=gparent,
                                key=sd.key,
                                defaults=dict(scoredef=sd, label=sd.label, ordering=sd.ordering))
                    else:
                        g,cr = SubmissionScoreSet.objects.get_or_create(
                                competition=comp,
                                key=sd.key,
                                defaults=dict(scoredef=sd, label=sd.label, ordering=sd.ordering))

                    # Associate the score definition with its leaderboard
                    sdg = SubmissionScoreDefGroup.objects.create(scoredef=sd, group=leaderboards[vals['leaderboard']['label']])

                for key, vals in comp_spec['leaderboard']['columns'].items():
                    index = comp_spec['leaderboard']['columns'].keys().index(key) + 1
                    # Only process the computed columns this time around.
                    if 'computed' not in vals:
                        continue
                    # Create the score definition
                    is_computed = True
                    sdefaults = {
                                    'label' : "" if 'label' not in vals else vals['label'].strip(),
                                    'numeric_format' : "2" if 'numeric_format' not in vals else vals['numeric_format'],
                                    'show_rank' : not is_computed,
                                    'sorting' : 'desc' if 'sort' not in vals else vals['sort'],
                                    'ordering' : index if 'rank' not in vals else vals['rank']
                                    }
                    if 'selection_default' in vals:
                        sdefaults['selection_default'] = vals['selection_default']

                    sd,cr = SubmissionScoreDef.objects.get_or_create(
                                competition=comp,
                                key=key,
                                computed=is_computed,
                                defaults=sdefaults)
                    sc,cr = SubmissionComputedScore.objects.get_or_create(scoredef=sd, operation=vals['computed']['operation'])
                    for f in vals['computed']['fields'].split(","):
                        f=f.strip()
                        # Note the lookup in brats_score_defs. The assumption is that computed properties are defined in
                        # brats_leaderboard_defs after the fields they reference.
                        # This is not a safe assumption -- given we can't control key/value ordering in a dictionary.
                        SubmissionComputedScoreField.objects.get_or_create(computed=sc, scoredef=columns[f])
                    columns[sd.key] = sd

                    # Associate the score definition with its column group
                    if 'column_group' in vals:
                        gparent = groups[vals['column_group']['label']]
                        g,cr = SubmissionScoreSet.objects.get_or_create(
                                competition=comp,
                                parent=gparent,
                                key=sd.key,
                                defaults=dict(scoredef=sd, label=sd.label, ordering=sd.ordering))
                    else:
                        g,cr = SubmissionScoreSet.objects.get_or_create(
                                competition=comp,
                                key=sd.key,
                                defaults=dict(scoredef=sd, label=sd.label, ordering=sd.ordering))

                    # Associate the score definition with its leaderboard
                    sdg = SubmissionScoreDefGroup.objects.create(scoredef=sd, group=leaderboards[vals['leaderboard']['label']])
                logger.debug("CompetitionDefBundle::unpack created scores (pk=%s)", self.pk)

        # Add owner as participant so they can view the competition
        approved = ParticipantStatus.objects.get(codename=ParticipantStatus.APPROVED)
        resulting_participant, created = CompetitionParticipant.objects.get_or_create(user=self.owner, competition=comp, defaults={'status':approved})
        logger.debug("CompetitionDefBundle::unpack added owner as participant (pk=%s)", self.pk)

        return comp

class SubmissionScoreDefGroup(models.Model):
    scoredef = models.ForeignKey(SubmissionScoreDef)
    group = models.ForeignKey(SubmissionResultGroup)

    class Meta:
        unique_together = (('scoredef','group'),)

    def __unicode__(self):
        return "%s %s" % (self.scoredef,self.group)

    def save(self,*args,**kwargs):
        if self.scoredef.competition != self.group.competition:
            raise IntegrityError("Score Def competition and phase compeition must be the same")
        super(SubmissionScoreDefGroup,self).save(*args,**kwargs)

class SubmissionComputedScore(models.Model):
    scoredef = models.OneToOneField(SubmissionScoreDef, related_name='computed_score')
    operation = models.CharField(max_length=10,choices=(('Max','Max'),
                                                        ('Avg', 'Average')))
class SubmissionComputedScoreField(models.Model):
    computed = models.ForeignKey(SubmissionComputedScore,related_name='fields')
    scoredef = models.ForeignKey(SubmissionScoreDef)

    def save(self,*args,**kwargs):
        if self.scoredef.computed is True:
            raise IntegrityError("Cannot use a computed field for a computed score")
        super(SubmissionComputedScoreField,self).save(*args,**kwargs)

class SubmissionScoreSet(MPTTModel):
    parent = TreeForeignKey('self',null=True,blank=True, related_name='children')
    competition = models.ForeignKey(Competition)
    key = models.CharField(max_length=50)
    label = models.CharField(max_length=50)
    scoredef = models.ForeignKey(SubmissionScoreDef,null=True,blank=True)
    ordering = models.PositiveIntegerField(default=1)

    class Meta:
        unique_together = (('key','competition'),)

    def __unicode__(self):
        return "%s %s" % (self.parent.label if self.parent else None, self.label)

class SubmissionScore(models.Model):
    result = models.ForeignKey(CompetitionSubmission, related_name='scores')
    scoredef = models.ForeignKey(SubmissionScoreDef)
    value = models.DecimalField(max_digits=20, decimal_places=10)

    class Meta:
        unique_together = (('result','scoredef'),)

    def save(self,*args,**kwargs):
        if self.scoredef.computed is True and self.value:
            raise IntegrityError("Score is computed. Cannot assign a value")
        super(SubmissionScore,self).save(*args,**kwargs)

class PhaseLeaderBoard(models.Model):
    phase = models.OneToOneField(CompetitionPhase,related_name='board')
    is_open = models.BooleanField(default=True)

    def submissions(self):
        return CompetitionSubmission.objects.filter(leaderboard_entry_result__board=self)

    def __unicode__(self):
        return "%s [%s]" % (self.phase.label,'Open' if self.is_open else 'Closed')

    def is_open(self):
        """
        The default implementation passes through the leaderboard is_open check to the phase is_active check.
        """
        self.is_open = self.phase.is_active
        return self.phase.is_active

    def scores(self,**kwargs):
        return self.phase.scores(score_filters=dict(result__leaderboard_entry_result__board=self))

class PhaseLeaderBoardEntry(models.Model):
    board = models.ForeignKey(PhaseLeaderBoard, related_name='entries')
    result = models.ForeignKey(CompetitionSubmission,  related_name='leaderboard_entry_result')

    class Meta:
        unique_together = (('board', 'result'),)


def dataset_data_file(dataset, filename="data.zip"):
    return os.path.join("datasets", str(dataset.pk), str(uuid.uuid4()), filename)


class OrganizerDataSet(models.Model):
    TYPES = (
        ("Reference Data", "Reference Data"),
        ("Scoring Program", "Scoring Program"),
        ("Input Data", "Input Data"),
        ("None", "None")
    )
    name = models.CharField(max_length=255)
    type = models.CharField(max_length=64, choices=TYPES, default="None")
    description = models.TextField(null=True, blank=True)
    data_file = models.FileField(
        upload_to=dataset_data_file,
        storage=BundleStorage,
        verbose_name="Data file",
        blank=True,
        null=True,
    )
    sub_data_files = models.ManyToManyField('OrganizerDataSet', null=True, blank=True, verbose_name="Bundle of data files")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL)
    key = UUIDField(version=4)

    def save(self, **kwargs):
        if self.key is None or self.key == '':
            self.key = "%s" % (uuid.uuid4())
        super(OrganizerDataSet, self).save(**kwargs)

    def __unicode__(self):
        return "%s uploaded by %s" % (self.name, self.uploaded_by)


class CompetitionSubmissionMetadata(models.Model):
    submission = models.ForeignKey(CompetitionSubmission, related_name="metadatas")
    is_predict = models.BooleanField(default=False)
    is_scoring = models.BooleanField(default=False)
    hostname = models.CharField(max_length=255, blank=True, null=True)
    processes_running_in_temp_dir = models.TextField(blank=True, null=True)

    beginning_virtual_memory_usage = models.TextField(blank=True, null=True)
    beginning_swap_memory_usage = models.TextField(blank=True, null=True)
    beginning_cpu_usage = models.TextField(blank=True, null=True)
    end_virtual_memory_usage = models.TextField(blank=True, null=True)
    end_swap_memory_usage = models.TextField(blank=True, null=True)
    end_cpu_usage = models.TextField(blank=True, null=True)

    def _get_json_property_percent(self, name):
        if hasattr(self, name):
            try:
                value = json.loads(getattr(self, name))["percent"]
                return "%s%%" % value
            except:
                return 'ERR!'
        return ''

    def _get_property_percent(self, name):
        value = getattr(self, name)
        return "%s%%" % value if value else None

    @property
    def simple(self):
        '''Returns the simplified versions of some of the metrics, i.e. just "percent" even though many more
        details are available.'''
        return {
            "processes_running_in_temp_dir": self.processes_running_in_temp_dir,
            "beginning_virtual_memory_usage": self._get_json_property_percent('beginning_virtual_memory_usage'),
            "beginning_swap_memory_usage":  self._get_json_property_percent('beginning_swap_memory_usage'),
            "beginning_cpu_usage": self._get_property_percent('beginning_cpu_usage'),
            "end_virtual_memory_usage": self._get_json_property_percent('end_virtual_memory_usage'),
            "end_swap_memory_usage": self._get_json_property_percent('end_swap_memory_usage'),
            "end_cpu_usage": self._get_property_percent('end_cpu_usage'),
        }


def add_submission_to_leaderboard(submission):
    """
    Adds the given submission to its leaderboard. It is the caller responsiblity to make
    sure the submission is ready to be added (e.g. it's in the finished state).
    """
    lb, _ = PhaseLeaderBoard.objects.get_or_create(phase=submission.phase)

    logger.info('Adding submission %s to leaderboard %s' % (submission, lb))

    # Currently we only allow one submission into the leaderboard although the leaderboard
    # is setup to accept multiple submissions from the same participant.
    entries = PhaseLeaderBoardEntry.objects.filter(board=lb, result__participant=submission.participant)
    for entry in entries:
        entry.delete()
    lbe, created = PhaseLeaderBoardEntry.objects.get_or_create(board=lb, result=submission)
    return lbe, created
