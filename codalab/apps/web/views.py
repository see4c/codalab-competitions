import csv
import datetime
import json
import os
import StringIO
import sys
import traceback
import yaml
import zipfile

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.sites.models import Site
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.core.servers.basehttp import FileWrapper
from django.core.urlresolvers import reverse
from django.db.models import Q
from django.http import Http404
from django.http import HttpResponse, HttpResponseRedirect
from django.http import StreamingHttpResponse
from django.shortcuts import render_to_response, render
from django.template import RequestContext, loader
from django.utils.decorators import method_decorator
from django.utils.html import strip_tags
from django.views.generic import View, TemplateView, DetailView, ListView, FormView, UpdateView, CreateView, DeleteView


from mimetypes import MimeTypes

from apps.web import forms
from apps.web import models
from apps.web import tasks
from apps.web.bundles import BundleService
from apps.coopetitions.models import Like, Dislike
from apps.forums.models import Forum
from apps.common.competition_utils import get_most_popular_competitions, get_featured_competitions
from tasks import evaluate_submission
from apps.teams.models import TeamMembership, get_user_team, get_competition_teams, get_competition_pending_teams, get_competition_deleted_teams


from extra_views import CreateWithInlinesView, UpdateWithInlinesView, InlineFormSet, NamedFormsetsMixin
from extra_views import generic
try:
    import azure
    import azure.storage
except ImportError:
    raise ImproperlyConfigured(
        "Could not load Azure bindings. "
        "See https://github.com/WindowsAzure/azure-sdk-for-python")

User = get_user_model()

############################################################
# General: template views


class HomePageView(TemplateView):
    template_name = "web/index.html"

    def get_context_data(self, **kwargs):
        context = super(HomePageView, self).get_context_data(**kwargs)

        c_key = 'popular_competitions'
        popular_competitions = cache.get(c_key)
        if not popular_competitions:
            popular_competitions = get_most_popular_competitions()
            cache.set(c_key, popular_competitions, 60 * 60 * 1)

        context['latest_competitions'] = popular_competitions
        context['featured_competitions'] = get_featured_competitions()

        return context


class LoginRequiredMixin(object):
    @method_decorator(login_required)
    def dispatch(self, *args, **kwargs):
        return super(LoginRequiredMixin, self).dispatch(*args, **kwargs)

class UserSettingsView(LoginRequiredMixin, UpdateView):
    template_name = "web/my/settings.html"
    form_class = forms.UserSettingsForm
    model = User
    success_url = "/my/settings/"

    def get_object(self, queryset=None):
        return self.request.user

############################################################
# Competitions: template views

def competition_index(request):
    query = request.GET.get('q')
    is_active = request.GET.get('is_active', False)
    is_finished = request.GET.get('is_finished', False)
    medical_image_viewer = request.GET.get('medical_image_viewer', False)

    competitions = models.Competition.objects.filter(published=True)

    if query:
        competitions = competitions.filter(Q(title__iregex=".*%s" % query) | Q(description__iregex=".*%s" % query))
    if medical_image_viewer:
        competitions = competitions.filter(enable_medical_image_viewer=True)
    if is_active:
        competitions = [c for c in competitions if c.is_active]
    if is_finished:
        competitions = [c for c in competitions if not c.is_active]

    competitions = reversed(sorted(competitions, key=lambda c: c.get_start_date))

    return render(request, "web/competitions/index.html", {
        'competitions': competitions,
    })

@login_required
def my_index(request):
    template = loader.get_template("web/my/index.html")
    try:
        denied = models.ParticipantStatus.objects.get(codename=models.ParticipantStatus.DENIED)
    except:
        denied = -1

    my_competitions = models.Competition.objects.filter(Q(creator=request.user) | Q(admins__in=[request.user])).order_by('-pk').select_related('creator')
    published_competitions = models.Competition.objects.filter(published=True).select_related('creator', 'participants')
    published_competitions = reversed(sorted(published_competitions, key=lambda c: c.get_start_date))
    context_dict = {
        'my_competitions': my_competitions,
        'competitions_im_in': list(request.user.participation.all().exclude(status=denied).select_related('creator')),
        'published_competitions': published_competitions,
        #'my_datasets': models.OrganizerDataSet.objects.filter()
    }
    return HttpResponse(template.render(RequestContext(request, context_dict)))

def sort_data_table(request, context, list):
    context['order'] = order = request.GET.get('order') if 'order' in request.GET else 'id'
    context['direction'] = direction = request.GET.get('direction') if 'direction' in request.GET else 'asc'
    reverse = direction == 'desc'
    def sortkey(x):
        return x[order] if order in x and x[order] is not None else ''
    list.sort(key=sortkey, reverse=reverse)

#
# Competition Views
#

class PhasesInline(InlineFormSet):
    model = models.CompetitionPhase
    form_class = forms.CompetitionPhaseForm
    extra = 0

class PagesInline(InlineFormSet):
    model = models.Page
    form_class = forms.PageForm
    extra = 0


class LeaderboardInline(InlineFormSet):
    model = models.SubmissionScoreDef
    form_class = forms.LeaderboardForm
    extra = 0


class CompetitionUpload(LoginRequiredMixin, CreateView):
    model = models.CompetitionDefBundle
    template_name = 'web/competitions/upload_competition.html'

class CompetitionEdit(LoginRequiredMixin, NamedFormsetsMixin, UpdateWithInlinesView):
    model = models.Competition
    form_class = forms.CompetitionForm
    inlines = [PagesInline, PhasesInline, LeaderboardInline]
    inlines_names = ['Pages', 'Phases', 'Leaderboards']
    template_name = 'web/competitions/edit.html'

    def forms_valid(self, form, inlines):
        form.instance.modified_by = self.request.user

        # save up here, before checks for new phase data
        save_result = super(CompetitionEdit, self).forms_valid(form, inlines)

        # inlines[0] = pages
        # inlines[1] = phases
        for phase_form in inlines[1]:
            if phase_form.cleaned_data["input_data_organizer_dataset"]:
                phase_form.instance.input_data = phase_form.cleaned_data["input_data_organizer_dataset"].data_file.file.name

            if phase_form.cleaned_data["reference_data_organizer_dataset"]:
                phase_form.instance.reference_data = phase_form.cleaned_data["reference_data_organizer_dataset"].data_file.file.name

            if phase_form.cleaned_data["scoring_program_organizer_dataset"]:
                phase_form.instance.scoring_program = phase_form.cleaned_data["scoring_program_organizer_dataset"].data_file.file.name

            phase_form.instance.save()

        # Look for admins that are not participants yet
        approved_status = models.ParticipantStatus.objects.get(codename=models.ParticipantStatus.APPROVED)

        for admin in form.instance.admins.all():
            try:
                participant = models.CompetitionParticipant.objects.get(user=admin, competition=form.instance)
                participant.status = approved_status
                participant.save()
            except ObjectDoesNotExist:
                models.CompetitionParticipant.objects.create(user=admin, competition=form.instance, status=approved_status)

        return save_result

    def get_context_data(self, **kwargs):
        context = super(CompetitionEdit, self).get_context_data(**kwargs)
        return context

    def construct_inlines(self):
        '''I need to overwrite this method in order to change
        the queryset for the "keywords" field'''
        inline_formsets = super(CompetitionEdit, self).construct_inlines()

        # inline_formsets[1] == phases
        for inline_form in inline_formsets[1].forms:
            # get existing datasets and add them, so admins can see them!
            input_data_ids = models.CompetitionPhase.objects.filter(competition=self.object).values_list('input_data_organizer_dataset')
            reference_data_ids = models.CompetitionPhase.objects.filter(competition=self.object).values_list('reference_data_organizer_dataset')
            scoring_program_ids = models.CompetitionPhase.objects.filter(competition=self.object).values_list('scoring_program_organizer_dataset')

            inline_form.fields['input_data_organizer_dataset'].queryset = models.OrganizerDataSet.objects.filter(
                Q(uploaded_by=self.request.user, type="Input Data") | Q(pk__in=input_data_ids)
            )
            inline_form.fields['reference_data_organizer_dataset'].queryset = models.OrganizerDataSet.objects.filter(
                Q(uploaded_by=self.request.user, type="Reference Data") | Q(pk__in=reference_data_ids)
            )
            inline_form.fields['scoring_program_organizer_dataset'].queryset = models.OrganizerDataSet.objects.filter(
                Q(uploaded_by=self.request.user, type="Scoring Program") | Q(pk__in=scoring_program_ids)
            )
        return inline_formsets

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()

        if self.object.creator != request.user and request.user not in self.object.admins.all():
            return HttpResponse(status=403)

        return super(CompetitionEdit, self).get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()

        if self.object.creator != request.user and request.user not in self.object.admins.all():
            return HttpResponse(status=403)

        return super(CompetitionEdit, self).post(request, *args, **kwargs)

class CompetitionDelete(LoginRequiredMixin, DeleteView):
    model = models.Competition
    template_name = 'web/competitions/confirm-delete.html'
    success_url = '/my/#manage'

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()

        if self.object.creator != request.user:
            return HttpResponse(status=403)

        context = self.get_context_data(object=self.object)
        return self.render_to_response(context)

    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()

        if self.object.creator != request.user:
            return HttpResponse(status=403)

        success_url = self.get_success_url()
        self.object.delete()
        return HttpResponseRedirect(success_url)


@login_required
def competition_message_participants(request, competition_id):
    if request.method != "POST":
        return HttpResponse(status=400)

    try:
        competition = models.Competition.objects.get(pk=competition_id)
    except ObjectDoesNotExist:
        return HttpResponse(status=404)

    if competition.creator != request.user and request.user not in competition.admins.all():
        return HttpResponse(status=403)

    if "subject" not in request.POST and "body" not in request.POST:
        return HttpResponse(
            json.dumps({
                "error": "Missing subject or body of message!"
            }),
            status=400
        )

    participants = models.CompetitionParticipant.objects.filter(
        competition=competition,
        status=models.ParticipantStatus.objects.get(codename="approved"),
        user__organizer_direct_message_updates=True
    )
    emails = [p.user.email for p in participants]
    subject = request.POST.get('subject')
    body = strip_tags(request.POST.get('body'))

    if len(emails) > 0:
        tasks.send_mass_email(
            competition,
            subject=subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to_emails=emails
        )

    return HttpResponse(status=200)


class UserDetailView(DetailView):
    model = User
    template_name = 'web/user_detail.html'

    def get_context_data(self, **kwargs):
        context_data = super(UserDetailView, self).get_context_data(**kwargs)
        context_data['information'] = {
            'Organization': self.object.organization_or_affiliation,
            'Team Name': self.object.team_name,
            'Team Members': self.object.team_members,
            'Method Name': self.object.method_name,
            'Method Description': self.object.method_description,
            'Contact Email': self.object.contact_email,
            'Project URL': self.object.project_url,
            'Publication URL': self.object.publication_url,
            'Bibtex': self.object.bibtex,
            'Public Profile': self.object.public_profile,
            'Biography': self.object.biography,
            'Web Page': self.object.webpage,
            'ORCID': self.object.ORCID,
            'Linkedin': self.object.linkedin,
            'Image': self.object.image,
        }
        return context_data


class CompetitionDetailView(DetailView):
    queryset = models.Competition.objects.all()
    model = models.Competition
    template_name = 'web/competitions/view.html'

    def get(self, request, *args, **kwargs):
        competition = self.get_object()
        secret_key = request.GET.get("secret_key", None)
        if competition.creator != request.user and request.user not in competition.admins.all():
            # user may not be logged in, so grab PK if we can, to check if they are a participant
            user_pk = request.user.pk or -1
            if not competition.participants.filter(user=user_pk).exists():
                if not competition.published and competition.secret_key != secret_key:
                    return HttpResponse(status=404)
        # FIXME: handles legacy problem with missing post_save signal for forums, creates forum if it
        # does not exist for this competition. should be removed eventually.
        if not hasattr(competition, 'forum'):
            Forum.objects.get_or_create(competition=competition)
        return super(CompetitionDetailView, self).get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(CompetitionDetailView, self).get_context_data(**kwargs)
        competition = context['object']
        all_phases = competition.phases.all()

        # This assumes the tabs were created in the correct order
        # TODO Add a rank, order by on ContentCategory
        side_tabs = dict()
        for category in models.ContentCategory.objects.all():
            pagecontent = context['object'].pagecontent
            if pagecontent is not None:
                tc = [x for x in pagecontent.pages.filter(category=category)]
            else:
                tc = []
            side_tabs[category] = tc

        context['tabs'] = side_tabs
        context['site'] = Site.objects.get_current()
        context['current_server_time'] = datetime.datetime.now()
        # context['public_submissions'] = []

        # c_key = "c%s_public_submissions" % competition.id
        # public_submissions = cache.get(c_key)
        # if not public_submissions:
        #     public_submissions = models.CompetitionSubmission.objects.filter(phase__competition=competition,
        #                                                                      is_public=True,
        #                                                                      status__codename="finished").prefetch_related()
        #     cache.set(c_key, public_submissions, 60 * 60 * 1)# Caching for an hour
        # for submission in public_submissions:
        #     # Let's process all public submissions and figure out which ones we've already liked
        #     if self.request.user.is_authenticated():
        #         if Like.objects.filter(submission=submission, user=self.request.user).exists():
        #             submission.already_liked = True
        #         if Dislike.objects.filter(submission=submission, user=self.request.user).exists():
        #             submission.already_disliked = True
        #     context['public_submissions'].append(submission)

        submissions = dict()
        # all_submissions = dict() REMOVE AFTER TESTING
        try:
            context["previous_phase"] = None
            context["next_phase"] = None
            context["first_phase"] = None
            phase_iterator = iter(all_phases)
            for phase in phase_iterator:
                if context["first_phase"] is None:
                    # Set the first phase if it hasn't been saved yet
                    context["first_phase"] = phase

                if phase.is_active:
                    context['active_phase'] = phase
                    # Set next phase if available
                    try:
                        context["next_phase"] = next(phase_iterator)
                    except StopIteration:
                        pass
                elif "active_phase" not in context:
                    # Set trailing phase since active one hasn't been found yet
                    context["previous_phase"] = phase

            # c_key = "c%s_all_participants" % competition.id
            # all_participants = cache.get(c_key)
            # if not all_participants:
            all_participants = competition.participants.all().select_related('user')
            # cache.set(c_key, all_participants, 60 * 5)# Caching for five minutes
            if self.request.user.is_authenticated() and self.request.user in [x.user for x in all_participants]:
                context['my_status'] = [x.status for x in all_participants if x.user == self.request.user][0].codename
                context['my_participant'] = competition.participants.get(user=self.request.user)
                phase_iterator = iter(all_phases)
                for phase in phase_iterator:
                    submissions[phase] = models.CompetitionSubmission.objects.filter(participant=context['my_participant'], phase=phase)
                    if phase.is_active:
                        context['my_active_phase_submissions'] = submissions[phase]
                context['my_submissions'] = submissions
            else:
                context['my_status'] = "unknown"
                for phase in all_phases:
                    if phase.is_active:
                        context['active_phase'] = phase
                # TODO, remove this in the future after testing.
                # Context is not being used anywhere
                #     all_submissions[phase] = phase.submissions.all()
                # context['active_phase_submissions'] = all_submissions

        except ObjectDoesNotExist:
            pass

        if competition.creator == self.request.user or self.request.user in competition.admins.all():
            context['is_admin_or_owner'] = True

        # Use this flag to trigger container-fluid for result table
        context['on_competition_detail'] = True

        return context


class CompetitionSubmissionsPage(LoginRequiredMixin, TemplateView):
    # Serves the table of submissions in the Participate tab of a competition.
    # Requires an authenticated user who is an approved participant of the competition.
    template_name = 'web/competitions/_submit_results_page.html'

    def get_context_data(self, **kwargs):
        context = super(CompetitionSubmissionsPage, self).get_context_data(**kwargs)
        context['phase'] = None
        competition = models.Competition.objects.get(pk=self.kwargs['id'])
        #if self.request.user in [x.user for x in competition.participants.all()]:
        if competition.participants.filter(user__in=[self.request.user]).exists():
            participant = competition.participants.get(user=self.request.user)
            if participant.status.codename == models.ParticipantStatus.APPROVED:
                phase = competition.phases.get(pk=self.kwargs['phase'])

                submissions = models.CompetitionSubmission.objects.filter(
                    participant=participant,
                    phase=phase
                ).select_related('status').order_by('submitted_at')

                # find which submission is in the leaderboard, if any and only if phase allows seeing results.
                id_of_submission_in_leaderboard = -1
                if not phase.is_blind:
                    leaderboard_entry = models.PhaseLeaderBoardEntry.objects.filter(
                        board__phase=phase,
                        result__participant__user=self.request.user
                    ).select_related('result', 'result__participant')
                    if leaderboard_entry:
                        id_of_submission_in_leaderboard = leaderboard_entry[0].result.pk
                submission_info_list = []
                for submission in submissions:
                    submission_info = {
                        'id': submission.id,
                        'number': submission.submission_number,
                        'filename': submission.get_filename(),  # left as call for legacy update of readable_filename on subs.
                        'submitted_at': submission.submitted_at,
                        'status_name': submission.status.name,
                        'is_finished': submission.status.codename == 'finished',
                        'is_in_leaderboard': submission.id == id_of_submission_in_leaderboard,
                        'exception_details': submission.exception_details,
                        'description': submission.description,
                        'team_name': submission.team_name,
                        'method_name': submission.method_name,
                        'method_description': submission.method_description,
                        'project_url': submission.project_url,
                        'publication_url': submission.publication_url,
                        'bibtex': submission.bibtex,
                        'organization_or_affiliation': submission.organization_or_affiliation,
                        'is_public': submission.is_public,
                    }
                    submission_info_list.append(submission_info)
                context['submission_info_list'] = submission_info_list
                context['phase'] = phase

        try:
            last_submission = models.CompetitionSubmission.objects.filter(
                participant__user=self.request.user,
                phase=context['phase']
            ).latest('submitted_at')
            context['last_submission_team_name'] = last_submission.team_name
            context['last_submission_method_name'] = last_submission.method_name
            context['last_submission_method_description'] = last_submission.method_description
            context['last_submission_project_url'] = last_submission.project_url
            context['last_submission_publication_url'] = last_submission.publication_url
            context['last_submission_bibtex'] = last_submission.bibtex
            context['last_submission_organization_or_affiliation'] = last_submission.organization_or_affiliation
        except ObjectDoesNotExist:
            pass
        return context


@login_required()
def competition_submission_metadata_page(request, competition_id, phase_id):
    try:
        competition = models.Competition.objects.get(pk=competition_id)
        selected_phase = competition.phases.get(pk=phase_id)
    except ObjectDoesNotExist:
        raise Http404()

    if request.user.id != competition.creator_id and request.user not in competition.admins.all():
            raise Http404()

    return render(request, "web/competitions/submission_metadata.html", {
        'competition': competition,
        'selected_phase': selected_phase,
        'stretch_100_percent_width': True
    })


class CompetitionResultsPage(TemplateView):
    # Serves the leaderboards in the Results tab of a competition.
    template_name = 'web/competitions/_results_page.html'

    def get_context_data(self, **kwargs):
        context = super(CompetitionResultsPage, self).get_context_data(**kwargs)
        try:
            competition = models.Competition.objects.get(pk=self.kwargs['id'])
            phase = competition.phases.get(pk=self.kwargs['phase'])
            is_owner = self.request.user.id == competition.creator_id
            context['is_owner'] = is_owner
            context['phase'] = phase
            context['groups'] = phase.scores()
            return context
        except:
            context['error'] = traceback.format_exc()
            return context


class CompetitionPublicSubmission(TemplateView):
    '''
    Returns the public  submissions of a competition
    1. Gets the competiton first base on the id
    2. Return competition as part of the context. It will be needed on the template
    '''
    template_name = 'web/competitions/public_submissions.html'

    def get_context_data(self, **kwargs):
        context = super(CompetitionPublicSubmission, self).get_context_data(**kwargs)
        try:
            competition = models.Competition.objects.get(pk=self.kwargs['pk'])
            context['competition'] = competition
        except:
            context['error'] = traceback.print_exc()

        return context


class CompetitionPublicSubmissionByPhases(TemplateView):
    '''
    Returns the submissions of a specif phase for a specifi competition
    1. We need the competition pk/id
    2. We need to phase competion pk/id
    3. We need to return public submissions
    4. We are using a Ajax request for this. Look into 'public_submissions.html' for more info
    5. Then we will append the results to '_public_submissions_phases.html'. Look at the file for more info
    '''
    template_name = 'web/competitions/public_submissions_phase.html'

    def get_context_data(self, **kwargs):

        context = super(CompetitionPublicSubmissionByPhases, self).get_context_data(**kwargs)
        try:
            competition = models.Competition.objects.get(pk=self.kwargs['pk'])
            competition_phase = self.kwargs['phase']
            context['competition'] = competition
            context['public_submissions'] = []
            public_submissions = models.CompetitionSubmission.objects.filter(phase__competition=competition,
                                                                             phase__pk = competition_phase,
                                                                             is_public=True,
                                                                             status__codename="finished").select_related('participant__user').prefetch_related('phase')
            # cache.set(c_key, public_submissions, 60 * 60 * 1)# Caching for an hour
            for submission in public_submissions:
                # Let's process all public submissions and figure out which ones we've already liked

                if self.request.user.is_authenticated():
                    if Like.objects.filter(submission=submission, user=self.request.user).exists():
                        submission.already_liked = True
                    if Dislike.objects.filter(submission=submission, user=self.request.user).exists():
                        submission.already_disliked = True
                context['public_submissions'].append(submission)
        except:
            context['error'] = traceback.print_exc()
        return context


class CompetitionCheckMigrations(View):
    def get(self, request, *args, **kwargs):
        competitions = models.Competition.objects.filter(is_migrating=False, published=True)

        for c in competitions:
            c.check_future_phase_sumbmissions()

        return HttpResponse()


class CompetitionResultsDownload(View):

    def get(self, request, *args, **kwargs):
        competition = models.Competition.objects.get(pk=self.kwargs['id'])
        phase = competition.phases.get(pk=self.kwargs['phase'])
        if phase.is_blind:
            return HttpResponse(status=403)
        response = HttpResponse(competition.get_results_csv(phase.pk), status=200, content_type="text/csv")
        response["Content-Disposition"] = "attachment; filename=%s results.csv" % phase.competition.title
        return response


class CompetitionCompleteResultsDownload(View):

    def get(self, request, *args, **kwargs):
        competition = models.Competition.objects.get(pk=self.kwargs['id'])
        phase = competition.phases.get(pk=self.kwargs['phase'])
        if phase.is_blind:
            return HttpResponse(status=403)
        groups = phase.scores(include_scores_not_on_leaderboard=True)
        leader_board = models.PhaseLeaderBoard.objects.get(phase=phase)

        csvfile = StringIO.StringIO()
        csvwriter = csv.writer(csvfile)

        for group in groups:
            csvwriter.writerow([group['label'].encode("utf-8")])
            csvwriter.writerow([])

            headers = ["User"]
            sub_headers = [""]
            for header in group['headers']:
                subs = header['subs']
                if subs:
                    for sub in subs:
                        headers.append(header['label'].encode("utf-8"))
                        sub_headers.append(sub['label'].encode("utf-8"))
                else:
                    headers.append(header['label'].encode("utf-8"))
            headers.append('Description')
            headers.append('Date')
            headers.append('Filename')
            headers.append('Is on leaderboard?')
            csvwriter.writerow(headers)
            csvwriter.writerow(sub_headers)

            if len(group['scores']) <= 0:
                csvwriter.writerow(["No data available"])
            else:
                leader_board_entries = models.PhaseLeaderBoardEntry.objects.filter(board=leader_board).values_list('result__id', flat=True)

                for pk, scores in group['scores']:
                    submission = models.CompetitionSubmission.objects.get(pk=scores['id'])
                    row = [scores['username']]
                    for v in scores['values']:
                        if 'rnk' in v:
                            row.append("%s (%s)" % (v['val'], v['rnk']))
                        else:
                            row.append("%s (%s)" % (v['val'], v['hidden_rnk']))

                    row.append(submission.description)
                    row.append(submission.submitted_at)
                    row.append(submission.get_filename())

                    is_on_leaderboard = submission.pk in leader_board_entries
                    row.append(is_on_leaderboard)

                    row = [unicode(r).encode("utf-8") for r in row]
                    csvwriter.writerow(row)

            csvwriter.writerow([])
            csvwriter.writerow([])

        response = HttpResponse(csvfile.getvalue(), status=200, content_type="text/csv")
        response["Content-Disposition"] = "attachment; filename=competition_results.csv"
        return response

### Views for My Codalab

class MyIndex(LoginRequiredMixin):
    pass

class MyCompetitionParticipantView(LoginRequiredMixin, ListView):
    queryset = models.CompetitionParticipant.objects.all()
    template_name = 'web/my/participants.html'

    def get_context_data(self, **kwargs):
        context = super(MyCompetitionParticipantView, self).get_context_data(**kwargs)
        # create column definition
        columns = [
            {
                'label': '#',
                'name': 'number'
            },
            {
                'label': 'NAME',
                'name': 'name'
            },
            {
                'label': 'EMAIL',
                'name': 'email'
            },
            {
                'label': 'TEAM',
                'name' : 'team_name'
            },
            {
                'label': 'STATUS',
                'name': 'status'
            },
            {
                'label': 'ENTRIES',
                'name': 'entries'
            }
        ]
        team_columns = [
            {
                'label': '#',
                'name': 'number'
            },
            {
                'label': 'NAME',
                'name': 'name'
            },
            {
                'label': 'CREATOR',
                'name': 'creator'
            },
            {
                'label': '# MEMBERS',
                'name': 'num_members'
            },
            {
                'label': 'PENDING REQ/INV',
                'name': 'num_pending'
            },
            {
                'label': 'STATUS',
                'name': 'status'
            },
            {
                'label': 'ENTRIES',
                'name': 'entries'
            }
        ]
        try:
            competition = models.Competition.objects.get(pk=self.kwargs.get('competition_id'))
        except models.Competition.DoesNotExist:
            raise Http404()

        if competition.creator != self.request.user and self.request.user not in competition.admins.all():
            raise Http404()

        context['columns'] = columns
        context['team_columns'] = team_columns

        # retrieve participant submissions information
        participant_list = []
        competition_participants = self.queryset.filter(competition=competition)
        competition_participants_ids = list(participant.id for participant in competition_participants)
        context['pending_participants'] = filter(lambda participant_submission: participant_submission.status.codename == models.ParticipantStatus.PENDING, competition_participants)
        participant_submissions = models.CompetitionSubmission.objects.filter(participant__in=competition_participants_ids)
        for number, participant in enumerate(competition_participants):
            team = get_user_team(participant, participant.competition)
            if team is not None:
                team_name=team.name
            else:
                team_name=''
            participant_entry = {
                'pk': participant.pk,
                'name': participant.user.username,
                'email': participant.user.email,
                'user_pk': participant.user.pk,
                'status': participant.status.codename,
                'number': number + 1,
                # equivalent to assigning participant.submissions.count() but without several multiple db queires
                'entries': len(filter(lambda participant_submission: participant_submission.participant.id == participant.id, participant_submissions)),
                'team_name': team_name,
                'team': team
            }
            participant_list.append(participant_entry)
        # order results
        sort_data_table(self.request, context, participant_list)
        context['participant_list'] = participant_list
        context['competition_id'] = self.kwargs.get('competition_id')
        competition = models.Competition.objects.get(pk=self.kwargs.get('competition_id'))

        if competition.enable_teams:
            context['teams_enabled'] = True;
            participant_memberships = TeamMembership.objects.filter(user__in=competition_participants_ids)
            teams_list=[]
            for number, team in enumerate(get_competition_teams(competition)):
                team_entry = {
                    'pk': team.pk,
                    'name': team.name,
                    'creator': team.creator.username,
                    'creator_pk': team.creator.pk,
                    'num_members': 0,
                    'num_pending': 0,
                    'status': team.status.codename,
                    'number': number + 1,
                    # equivalent to assigning participant.submissions.count() but without several multiple db queires
                    'entries': len(filter(lambda participant_submission: get_user_team(participant_submission.participant) == team, participant_submissions)),
                }
                teams_list.append(team_entry)
            context['team_list'] = teams_list
        context['pending_teams'] = get_competition_pending_teams(competition)
        return context

    def get_queryset(self):
        return self.queryset.filter(competition=self.kwargs.get('competition_id'))

## Partials

class CompetitionIndexPartial(TemplateView):

    def get_context_data(self, **kwargs):
        ## Currently gets all competitions
        context = super(CompetitionIndexPartial, self).get_context_data(**kwargs)
        per_page = self.request.GET.get('per_page', 6)
        page = self.request.GET.get('page', 1)
        clist = models.Competition.objects.all()

        pgn = Paginator(clist, per_page)
        try:
            competitions = pgn.page(page)
        except PageNotAnInteger:
            # If page is not an integer, deliver first page.
            competitions = pgn.page(1)
        except EmptyPage:
            # If page is out of range (e.g. 9999), deliver last page of results.
            competitions = []
        context['competitions'] = competitions
        return context

class MyCompetitionsManagedPartial(ListView):
    model = models.Competition
    template_name = 'web/my/_managed.html'
    queryset = models.Competition.objects.all()

    def get_queryset(self):
        return self.queryset.filter(creator=self.request.user)

class MyCompetitionsEnteredPartial(ListView):
    model = models.CompetitionParticipant
    template_name = 'web/my/_entered.html'
    queryset = models.CompetitionParticipant.objects.all()

    def get_queryset(self):
        return self.queryset.filter(user=self.request.user)

class MyCompetitionDetailsTab(TemplateView):
    template_name = 'web/my/_tab.html'

class MySubmissionResultsPartial(TemplateView):
    template_name = 'web/my/_submission_results.html'

    def get_context_data(self, **kwargs):
        ctx = super(MySubmissionResultsPartial, self).get_context_data(**kwargs)

        participant_id = kwargs.get('participant_id')
        participant = models.CompetitionParticipant.objects.get(pk=participant_id)

        phase_id = kwargs.get('phase_id')
        phase = models.CompetitionPhase.objects.get(pk=phase_id)

        ctx['active_phase'] = phase
        ctx['my_active_phase_submissions'] = phase.submissions.filter(participant=participant)

        return ctx


class MyCompetitionSubmissionToggleMakePublic(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        try:
            submission = models.CompetitionSubmission.objects.get(pk=kwargs.get('submission_id'))

            if request.user == submission.participant.user:
                submission.is_public = not submission.is_public
                submission.save()
                return HttpResponse(submission.is_public)
            else:
                raise Http404()
        except ObjectDoesNotExist:
            raise Http404()


class MyCompetitionSubmissionOutput(LoginRequiredMixin, View):
    """
    This view serves the files associated with a submission.
    """
    def get(self, request, *args, **kwargs):
        submission = models.CompetitionSubmission.objects.get(pk=kwargs.get('submission_id'))
        competition = submission.phase.competition

        # Check competition admin permissions or user permissions
        if submission.is_public:
            if competition.has_registration and not competition.participants.filter(user=request.user).exists():
                raise Http404()
        else:
            if (competition.creator != request.user and request.user not in competition.admins.all()) and \
                request.user != submission.participant.user:
                raise Http404()

        filetype = kwargs.get('filetype')
        try:
            file, file_type, file_name = submission.get_file_for_download(filetype, request.user)
        except PermissionDenied:
            return HttpResponse(status=403)
        except ValueError:
            return HttpResponse(status=400)
        except:
            return HttpResponse(status=500)
        try:
            response = HttpResponse(file.read(), status=200, content_type=file_type)
            if file_type == 'application/zip':
                response['Content-Type'] = 'application/zip'
                response['Content-Disposition'] = 'attachment; filename="{0}"'.format(file_name)
            else:
                response['Content-Type'] = file_type
            return response
        except azure.WindowsAzureMissingResourceError:
            # for stderr.txt which does not exist when no errors have occurred
            # this may hide a true 404 in unexpected circumstances
            return HttpResponse("", status=200, content_type='text/plain')
        except:
            # Let's check to make sure we're in a prediction competition, otherwise let user know
            if filetype.startswith("predict_") and submission.phase.is_scoring_only:
                return HttpResponse("This competition is scoring only, prediction data not available",
                                    content_type='text/plain')
            else:
                msg = "There was an error retrieving file '%s'. Please try again later or report the issue."
                return HttpResponse(msg % filetype, status=200, content_type='text/plain')

class MyCompetitionSubmissionDetailedResults(TemplateView):
    """
    This view serves the files associated with a submission.
    """
    model = models.CompetitionSubmission
    template_name = 'web/my/detailed_results.html'
    def get(self, request, *args, **kwargs):
        submission = models.CompetitionSubmission.objects.get(pk=kwargs.get('submission_id'))
        context_dict = {'id': kwargs.get('submission_id'), 'user': submission.participant.user, 'filename':submission.detailed_results_file.name}
        return render_to_response('web/my/detailed_results.html', context_dict, RequestContext(request))

class MyCompetitionSubmissionsPage(LoginRequiredMixin, TemplateView):
    # Serves the table of submissions in the submissions competition administration.
    # Requires an authenticated user who is an administrator of the competition.
    queryset = models.Competition.objects.all()
    model = models.Competition
    template_name = 'web/my/submissions.html'

    def get_context_data(self, **kwargs):
        phase_id = self.request.GET.get('phase')
        context = super(MyCompetitionSubmissionsPage, self).get_context_data(**kwargs)
        competition = models.Competition.objects.get(pk=self.kwargs['competition_id'])
        context['competition'] = competition

        if self.request.user.id != competition.creator_id and self.request.user not in competition.admins.all():
            raise Http404()

        # find the active phase
        if (phase_id != None):
            context['selected_phase_id'] = int(phase_id)
            active_phase = competition.phases.filter(id=phase_id)[0]
        else:
            phases = list(competition.phases.all())
            active_phase = phases[0]
            for phase in phases:
                if phase.is_active:
                    context['selected_phase_id'] = phase.id
                    active_phase = phase

        context['selected_phase'] = active_phase

        submissions = models.CompetitionSubmission.objects.filter(phase=active_phase).select_related('participant', 'participant__user', 'status')
        # find which submissions are in the leaderboard, if any and only if phase allows seeing results.
        leaderboard_entries = list(models.PhaseLeaderBoardEntry.objects.filter(board__phase__competition=competition))
        id_of_submissions_in_leaderboard = [e.result.id for e in leaderboard_entries if e.result in submissions]
        # create column definition
        columns = [
            {
                'label': 'SUBMITTED',
                'name': 'submitted_at'
            },
            {
                'label': 'SUBMITTED BY',
                'name': 'submitted_by'
            },
            {
                'label': 'SUBMISSION ID',
                'name': 'submission_pk'
            },
            {
                'label': 'FILENAME',
                'name': 'filename'
            },
            {
                'label': 'STATUS',
                'name': 'status_name'
            },
            {
                'label': 'LEADERBOARD',
                'name': 'is_in_leaderboard'
            },
        ]
        scores = active_phase.scores()
        for score_group_index, score_group in enumerate(scores):
            column = {
                'label': score_group['label'],
                'name': 'score_' + str(score_group_index),
            }
            columns.append(column)
        # map submissions to view data
        submission_info_list = []
        for submission in submissions:
            submission_info = {
                'id': submission.id,
                'submitted_by': submission.participant.user.username,
                'user_pk': submission.participant.user.pk,
                'number': submission.submission_number,
                'filename': submission.get_filename(),
                'submitted_at': submission.submitted_at,
                'status_name': submission.status.name,
                'is_in_leaderboard': submission.id in id_of_submissions_in_leaderboard,
                'exception_details': submission.exception_details,
                'description': submission.description,
                'is_public': submission.is_public,
                'submission_pk': submission.id,
                'is_migrated': submission.is_migrated
            }
            # add score groups into data columns
            if (submission_info['is_in_leaderboard'] == True):
                for score_group_index, score_group in enumerate(scores):
                    user_score = filter(lambda user_score: user_score[1]['username'] == submission.participant.user.username, score_group['scores'])[0]
                    main_score = filter(lambda main_score: main_score['name'] == score_group['selection_key'], user_score[1]['values'])[0]
                    submission_info['score_' + str(score_group_index)] = main_score['val']
            submission_info_list.append(submission_info)
        # order results
        sort_data_table(self.request, context, submission_info_list)
        # complete context
        context['columns'] = columns
        context['submission_info_list'] = submission_info_list

        # We need a way to check if next phase.auto_migration = True
        try:
            next_phase = competition.phases.get(phasenumber=submission.phase.phasenumber+1)
            context['next_phase'] = next_phase.auto_migration
        except Exception:
            sys.exc_clear()
        context['phase'] = active_phase

        return context

class VersionView(TemplateView):
    template_name = 'web/project_version.html'

    def get_context_data(self):
        import subprocess
        p = subprocess.Popen(["git", "rev-parse", "HEAD"], stdout=subprocess.PIPE)
        out, err = p.communicate()
        ctx = super(VersionView, self).get_context_data()
        ctx['commit_hash'] = out
        tasks.echo("version is " + out)
        return ctx

class OrganizerDataSetListView(LoginRequiredMixin, ListView):
    model = models.OrganizerDataSet
    template_name = "web/my/datasets.html"

    def get_queryset(self):
        return models.OrganizerDataSet.objects.filter(uploaded_by=self.request.user)


class OrganizerDataSetFormMixin(LoginRequiredMixin):
    model = models.OrganizerDataSet
    form_class = forms.OrganizerDataSetModelForm
    template_name = "web/my/datasets_form.html"

    def get_form_kwargs(self, **kwargs):
        kwargs = super(OrganizerDataSetFormMixin, self).get_form_kwargs(**kwargs)
        kwargs['user'] = self.request.user
        return kwargs

    def get_form(self, form_class):
        form = super(OrganizerDataSetFormMixin, self).get_form(form_class)
        form.fields["sub_data_files"].queryset = models.OrganizerDataSet.objects.filter(
            uploaded_by=self.request.user,
            sub_data_files__isnull=True, # ignore datasets that are multi
        )
        return form

    def get_success_url(self):
        return reverse("my_datasets")


class OrganizerDataSetCreate(OrganizerDataSetFormMixin, CreateView):
    model = models.OrganizerDataSet
    form_class = forms.OrganizerDataSetModelForm
    template_name = "web/my/datasets_form.html"

    def get_form_kwargs(self, **kwargs):
        kwargs = super(OrganizerDataSetCreate, self).get_form_kwargs(**kwargs)
        kwargs['user'] = self.request.user
        return kwargs

    def get_success_url(self):
        return reverse("my_datasets")


class OrganizerDataSetCheckOwnershipMixin(LoginRequiredMixin):
    def get_object(self, queryset=None):
        dataset = super(OrganizerDataSetCheckOwnershipMixin, self).get_object(queryset)

        if dataset.uploaded_by != self.request.user:
            raise Http404()

        return dataset


class OrganizerDataSetUpdate(OrganizerDataSetCheckOwnershipMixin, OrganizerDataSetFormMixin, UpdateView):
    pass


class OrganizerDataSetDelete(OrganizerDataSetCheckOwnershipMixin, DeleteView):
    model = models.OrganizerDataSet
    template_name = "web/my/datasets_delete.html"

    def get_success_url(self):
        return reverse("my_datasets")

    def get_context_data(self, **kwargs):
        context = super(OrganizerDataSetDelete, self).get_context_data(**kwargs)

        usage = models.Competition.objects.all()

        if self.object.type == "Input Data":
            usage = usage.filter(phases__input_data_organizer_dataset=self.object)
        elif self.object.type == "Reference Data":
            usage = usage.filter(phases__reference_data_organizer_dataset=self.object)
        elif self.object.type == "Scoring Program":
            usage = usage.filter(phases__scoring_program_organizer_dataset=self.object)
        else:
            usage = usage.filter(Q(phases__input_data_organizer_dataset=self.object) |
                                 Q(phases__reference_data_organizer_dataset=self.object) |
                                 Q(phases__scoring_program_organizer_dataset=self.object))

        # Filter out duplicates
        context["competitions_in_use"] = usage.distinct()
        return context


class SubmissionDelete(LoginRequiredMixin, DeleteView):
    model = models.CompetitionSubmission
    template_name = "web/my/submission_delete.html"

    def get_object(self, queryset=None):
        obj = super(SubmissionDelete, self).get_object(queryset)

        self.success_url = reverse("competitions:view", kwargs={"pk": obj.phase.competition.pk})

        if obj.participant.user != self.request.user and obj.phase.competition.creator != self.request.user:
            raise Http404()

        return obj


def download_dataset(request, dataset_key):
    try:
        dataset = models.OrganizerDataSet.objects.get(key=dataset_key)
    except ObjectDoesNotExist:
        raise Http404()

    try:
        if dataset.sub_data_files.count() > 0:
            # TODO: Could refactor this to only zip this stuff up one time, maybe after dataset creation?
            zip_buffer = StringIO.StringIO()

            zip_file = zipfile.ZipFile(zip_buffer, "w")
            file_name = ""

            for sub_dataset in dataset.sub_data_files.all():
                file_dir, file_name = os.path.split(sub_dataset.data_file.file.name)
                zip_file.writestr(file_name, sub_dataset.data_file.read())

            zip_file.close()

            resp = HttpResponse(zip_buffer.getvalue(), mimetype = "application/x-zip-compressed")
            resp['Content-Disposition'] = 'attachment; filename=%s.zip' % dataset.name
            return resp
        else:
            mime = MimeTypes()
            file_type = mime.guess_type(dataset.data_file.file.name)
            response = StreamingHttpResponse(
                FileWrapper(dataset.data_file.file, blksize=8192 * 100),
                status=200,
                content_type=file_type
            )
            response['Content-Length'] = dataset.data_file.file.size
            if file_type != 'text/plain':
                response['Content-Disposition'] = 'attachment; filename="{0}"'.format(dataset.data_file.file.name)
            return response
    except:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        print "*** print_tb:"
        traceback.print_tb(exc_traceback, limit=1, file=sys.stdout)
        print "*** print_exception:"
        traceback.print_exception(exc_type, exc_value, exc_traceback,
                                  limit=2, file=sys.stdout)
        print "*** print_exc:"
        traceback.print_exc()
        print "*** format_exc, first and last line:"
        formatted_lines = traceback.format_exc().splitlines()
        msg = "There was an error retrieving the file. Please try again later or report the issue."
        return HttpResponse(msg, status=400, content_type='text/plain')


def datasets_delete_multiple(request):
    ids_to_delete = request.POST.getlist("ids_to_delete[]", [])

    for dataset_id in ids_to_delete:
        try:
            dataset = models.OrganizerDataSet.objects.get(pk=int(dataset_id), uploaded_by=request.user)
            dataset.delete()
        except:
            pass

    return HttpResponse()


def download_competition_yaml(request, competition_pk):
    try:
        competition = models.Competition.objects.get(pk=competition_pk)

        if competition.creator != request.user and request.user not in competition.admins.all():
            return HttpResponse(status=403)

        response = HttpResponse(competition.original_yaml_file, content_type="text/yaml")
        response['Content-Disposition'] = 'attachment; filename="competition_%s.yaml"' % competition_pk
        return response
    except ObjectDoesNotExist:
        return HttpResponse(status=404)


@login_required
def download_competition_bundle(request, competition_pk):
    if not request.user.is_staff:
        return HttpResponse(status=403)

    try:
        competition = models.Competition.objects.get(pk=competition_pk)
    except ObjectDoesNotExist:
        raise Http404()

    try:
        zip_buffer = StringIO.StringIO()
        zip_file = zipfile.ZipFile(zip_buffer, "w")
        yaml_data = yaml.load(competition.original_yaml_file)

        # Grab logo
        zip_file.writestr(yaml_data["image"], competition.image.file.read())

        # Grab html pages
        for p in competition.pagecontent.pages.all():
            if p.codename in yaml_data["html"].keys() or p.codename == 'terms_and_conditions' or p.codename == 'get_data':
                if p.codename == 'terms_and_conditions':
                    # overwrite this for consistency
                    p.codename = 'terms'
                if p.codename == 'get_data':
                    # overwrite for consistency
                    p.codename = 'data'
                zip_file.writestr(yaml_data["html"][p.codename], p.html.encode("utf-8"))

        # Grab input data, reference data, scoring program
        file_name_cache = []

        for phase in competition.phases.all():
            for phase_index, phase_yaml in yaml_data["phases"].items():
                if phase_yaml["phasenumber"] == phase.phasenumber:
                    if phase.reference_data and phase.reference_data.file.name not in file_name_cache:
                        yaml_data["phases"][phase_index]["reference_data"] = phase.reference_data.file.name
                        file_name_cache += phase.reference_data.file.name
                        zip_file.writestr(phase.reference_data.file.name, phase.reference_data.file.read())

                    if phase.input_data and phase.input_data.file.name not in file_name_cache:
                        yaml_data["phases"][phase_index]["input_data"] = phase.input_data.file.name
                        file_name_cache += phase.input_data.file.name
                        zip_file.writestr(phase.input_data.file.name, phase.input_data.file.read())

                    if phase.scoring_program and phase.scoring_program.file.name not in file_name_cache:
                        yaml_data["phases"][phase_index]["scoring_program"] = phase.scoring_program.file.name
                        file_name_cache += phase.scoring_program.file.name
                        zip_file.writestr(phase.scoring_program.file.name, phase.scoring_program.file.read())

        zip_file.writestr("competition.yaml", yaml.dump(yaml_data))

        zip_file.close()

        resp = HttpResponse(zip_buffer.getvalue(), mimetype = "application/x-zip-compressed")
        resp['Content-Disposition'] = 'attachment; filename=%s-%s.zip' % (competition.title, competition.pk)
        return resp
    except:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        print "*** print_tb:"
        traceback.print_tb(exc_traceback, limit=1, file=sys.stdout)
        print "*** print_exception:"
        traceback.print_exception(exc_type, exc_value, exc_traceback,
                                  limit=2, file=sys.stdout)
        print "*** print_exc:"
        traceback.print_exc()
        print "*** format_exc, first and last line:"
        formatted_lines = traceback.format_exc().splitlines()
        msg = "There was an error retrieving the file. Please try again later or report the issue."
        return HttpResponse(msg, status=400, content_type='text/plain')



@login_required
def download_leaderboard_results(request, competition_pk, phase_pk):
    try:
        competition = models.Competition.objects.get(pk=competition_pk)
        if competition.creator != request.user and request.user not in competition.admins.all():
            raise Http404()

        phase = models.CompetitionPhase.objects.get(pk=phase_pk)
        leaderboard_entries = models.PhaseLeaderBoardEntry.objects.filter(board__phase=phase)
    except ObjectDoesNotExist:
        raise Http404()

    try:
        zip_buffer = StringIO.StringIO()
        zip_file = zipfile.ZipFile(zip_buffer, "w")

        # Add teach team name in an easy to read way
        team_name_cache = {}
        team_name_string = ""
        for result in models.PhaseLeaderBoardEntry.objects.filter(result__participant__user__team_name__isnull=False,
                                                                  result__participant__competition=competition):
            user_on_team = result.result.participant.user
            team_name_cache[user_on_team.team_name] = user_on_team.team_members
        for name, members in team_name_cache.items():
            team_name_string += "Team: %s; members: %s\n" % (name, members)

        if team_name_string:
            zip_file.writestr("team_names_and_members.txt", team_name_string.encode('utf8'))

        # Add each submission
        for entry in leaderboard_entries:
            submission = entry.result
            username_or_team_name = submission.participant.user.username if not submission.participant.user.team_name else "Team %s " % submission.participant.user.team_name
            file_name = "%s - %s submission.zip" % (username_or_team_name, submission.submission_number)
            zip_file.writestr(file_name, submission.file.read())

            output_file_name = "%s - %s output.zip" % (username_or_team_name, submission.submission_number)
            zip_file.writestr(output_file_name, submission.output_file.read())

            profile_data_file_name = "%s - %s profile.txt" % (username_or_team_name, submission.submission_number)

            team_name=submission.participant.user.team_name
            team_members=submission.participant.user.team_members

            if submission.team is not None:
                team_name=submission.team.name
                team_members=''
                for member in submission.team.get_members("approved"):
                    if len(team_members)>0:
                        team_members += "\0"
                    team_members += member.user.username + "<" + member.user.email + ">"

            user_profile_data = {
                'Organization': submission.participant.user.organization_or_affiliation,
                'Team Name': team_name,
                'Team Members': team_members,
                'Method Name': submission.participant.user.method_name,
                'Method Description': submission.participant.user.method_description,
                'Contact Email': submission.participant.user.contact_email,
                'Project URL': submission.participant.user.project_url,
                'Publication URL': submission.participant.user.publication_url,
                'Bibtex': submission.participant.user.bibtex,
            }
            user_profile_data_string = '\n'.join(['%s: %s' % (k, v) for k, v in user_profile_data.items()])
            zip_file.writestr(profile_data_file_name, user_profile_data_string.encode('utf-8'))

            metadata_fields = ['method_name', 'method_description', 'project_url', 'publication_url', 'bibtex', 'team_name', 'organization_or_affiliation']
            submission_metadata_file_name = "%s - %s method.txt" % (username_or_team_name, submission.submission_number)
            submission_metadata_file_string = "\n".join(["%s: %s" % (field, getattr(submission, field)) for field in metadata_fields])
            zip_file.writestr(submission_metadata_file_name, submission_metadata_file_string.encode('utf-8'))

        zip_file.close()

        resp = HttpResponse(zip_buffer.getvalue(), mimetype = "application/x-zip-compressed")
        resp['Content-Disposition'] = 'attachment; filename=%s-%s-results.zip' % (competition.title, competition.pk)
        return resp
    except:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        print "*** print_tb:"
        traceback.print_tb(exc_traceback, limit=1, file=sys.stdout)
        print "*** print_exception:"
        traceback.print_exception(exc_type, exc_value, exc_traceback,
                                  limit=2, file=sys.stdout)
        print "*** print_exc:"
        traceback.print_exc()
        print "*** format_exc, first and last line:"
        formatted_lines = traceback.format_exc().splitlines()
        msg = "There was an error retrieving the file. Please try again later or report the issue."
        return HttpResponse(msg, status=400, content_type='text/plain')


@login_required
def submission_update_description(request, submission_pk):
    try:
        submission = models.CompetitionSubmission.objects.get(pk=submission_pk)
        if submission.participant.user != request.user:
            raise Http404()
        submission.description = request.POST.get('updated_description')
        submission.save()
        return HttpResponse()
    except models.CompetitionSubmission.DoesNotExist:
        raise Http404()


@login_required
def submission_mark_as_failed(request, submission_pk):
    if request.method == "POST":
        try:
            submission = models.CompetitionSubmission.objects.get(pk=submission_pk)
            competition = submission.phase.competition
            if request.user.id != competition.creator_id and request.user not in competition.admins.all():
                raise Http404()
            submission.status = models.CompetitionSubmissionStatus.objects.get(codename="failed")
            submission.save()
            return HttpResponse()
        except models.CompetitionSubmission.DoesNotExist:
            raise Http404()
    raise Http404()


@login_required
def submission_toggle_leaderboard(request, submission_pk):
    if request.method == "POST":
        try:
            submission = models.CompetitionSubmission.objects.get(pk=submission_pk)
            competition = submission.phase.competition
            if request.user.id != competition.creator_id and request.user not in competition.admins.all():
                raise Http404()

            if submission.status.codename != "finished":
                return HttpResponse(status=400)

            is_on_leaderboard = models.PhaseLeaderBoardEntry.objects.filter(result=submission).exists()
            # If the submission isn't already on the leaderboard, then add it to it
            # otherwise delete it and other submissions else from leaderboard
            if not is_on_leaderboard:
                models.add_submission_to_leaderboard(submission)
            else:
                entries = models.PhaseLeaderBoardEntry.objects.filter(
                    board__phase=submission.phase,
                    result__participant=submission.participant
                )
                for entry in entries:
                    entry.delete()

            return HttpResponse()
        except models.CompetitionSubmission.DoesNotExist:
            raise Http404()
    raise Http404()


@login_required
def submission_re_run(request, submission_pk):
    if request.method == "POST":
        try:
            submission = models.CompetitionSubmission.objects.get(pk=submission_pk)
            competition = submission.phase.competition
            if request.user.id != competition.creator_id and request.user not in competition.admins.all():
                raise Http404()

            new_submission = models.CompetitionSubmission(
                participant=submission.participant,
                file=submission.file,
                phase=submission.phase
            )
            new_submission.save(ignore_submission_limits=True)

            evaluate_submission(new_submission.pk, submission.phase.is_scoring_only)

            return HttpResponse()
        except models.CompetitionSubmission.DoesNotExist:
            raise Http404()
    raise Http404()


@login_required
def submission_migrate(request, pk):
    '''
    Will allow to migrate to submissions manually to next phase
    '''
    if request.method == "POST":
        try:
            submission = models.CompetitionSubmission.objects.get(pk=pk)
            competition = submission.phase.competition
            if request.user.id != competition.creator.id and request.user not in competition.admins.all():
                raise Http404()

            current_phase_phasenumber = submission.phase.phasenumber
            next_phase = competition.phases.get(phasenumber=current_phase_phasenumber+1)

            new_submission = models.CompetitionSubmission(
                participant=submission.participant,
                file=submission.file,
                phase=next_phase)

            new_submission.save(ignore_submission_limits=True)

            evaluate_submission(new_submission.pk, submission.phase.is_scoring_only)
            submission.is_migrated = True
            submission.save()

            return HttpResponse()
        except models.CompetitionSubmission.DoesNotExist:
            raise Http404()
    raise Http404()

############################################################
# Worksheets: template views


class BundleDetailView(TemplateView):
    """
    Displays details for a bundle.
    """
    template_name = 'web/bundles/detail.html'
    def get_context_data(self, **kwargs):
        context = super(BundleDetailView, self).get_context_data(**kwargs)
        uuid = kwargs.get('uuid')
        service = BundleService(self.request.user)
        bundle_info = service.get_bundle_info(uuid)
        if bundle_info:
            context['bundle'] = bundle_info
            context['bundle_title'] = bundle_info.get('metadata', {}).get('name', '')
        else:
            context['error'] = 'Invalid or inaccessible bundle uuid: ' + uuid
        return context

def BundleDownload(request, uuid):
    '''
    Return a stream with the contents of the bundle (zip file if necessary).
    This is the same code as BundleFileContentApi.
    '''
    service = BundleService(request.user)
    stream, name, content_type = service.read_target((uuid, ''))
    response = StreamingHttpResponse(stream, content_type=content_type)
    response['Content-Disposition'] = 'filename="%s"' % name
    return response
