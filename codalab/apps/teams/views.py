from django.utils.timezone import now
from django.contrib.auth import get_user_model
from django.shortcuts import render, get_object_or_404
from django.views.generic import DetailView, TemplateView
from django.core.urlresolvers import reverse

from apps.web.models import Competition, ParticipantStatus
from apps.web.views import LoginRequiredMixin

from .models import Team, TeamStatus, TeamMembership, TeamMembershipStatus, get_user_requests, get_competition_teams, get_user_team, get_allowed_teams, get_team_pending_membership
from apps.teams import forms
from django.views.generic import View, TemplateView, DetailView, ListView, FormView, UpdateView, CreateView, DeleteView
from extra_views import CreateWithInlinesView, UpdateWithInlinesView, InlineFormSet, NamedFormsetsMixin

from django.db.models import Q
from django.http import Http404
from django.http import HttpResponse, HttpResponseRedirect, HttpResponseForbidden
from django.http import StreamingHttpResponse

User = get_user_model()

class TeamDetailView(LoginRequiredMixin, TemplateView):
    # Serves the table of submissions in the Participate tab of a competition.
    # Requires an authenticated user who is an approved participant of the competition.
    template_name = 'teams/team_info.html'

    def get_context_data(self, **kwargs):
        context = super(TeamDetailView, self).get_context_data(**kwargs)
        context['team'] = None
        competition = Competition.objects.get(pk=self.kwargs['competition_pk'])
        context['competition'] = competition

        members_columns = [
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
                'label': 'JOINED',
                'name' : 'joined'
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

        if competition.participants.filter(user__in=[self.request.user]).exists():
            participant = competition.participants.get(user=self.request.user)
            if participant.status.codename == ParticipantStatus.APPROVED:
                team_list= get_competition_teams(competition)
                user_requests = get_user_requests(participant, competition)
                user_team=get_user_team(participant, competition)
                if user_team is not None:
                    context['team'] = user_team
                    context['team_requests'] = get_team_pending_membership(user_team)
                    member_list=[]
                    for number, member in enumerate(user_team.members.all()):
                        membership = member.teammembership_set.get(team=user_team)
                        user_entry = {
                            'pk': member.pk,
                            'name': member.username,
                            'email': member.email,
                            'joined': membership.start_date,
                            'status': membership.status.codename,
                            'number': number + 1,
                            'entries': 0,
                        }
                        if user_entry['status'] == TeamMembershipStatus.APPROVED:
                            member_list.append(user_entry)
                    context['team_members']=member_list
                    context['members_columns'] = members_columns
                context['requests'] = user_requests
                context['teams'] = team_list
                context['allowed_teams'] = get_allowed_teams(participant, competition)


        return context

class RequestTeamView(TeamDetailView):
    def get_context_data(self, **kwargs):
        error = None
        action = self.kwargs['action']
        request = TeamMembership.objects.get(pk=self.kwargs['request_pk'])
        competition = Competition.objects.get(pk=self.kwargs['competition_pk'])
        if competition.participants.filter(user__in=[self.request.user]).exists():
            participant = competition.participants.get(user=self.request.user)
            if participant.status.codename == ParticipantStatus.APPROVED:
                if request.is_active:
                    if request.user==participant.user:
                        if action == 'accept':
                            if not request.is_invitation:
                                error="Invalid request type: Cannot accept your own request"
                            else:
                                request.is_accepted=True
                                request.save()
                        elif action == 'reject':
                            if not request.is_invitation:
                                error="Invalid request type: Cannot reject your own request"
                            else:
                                request.end_date=now()
                                request.save()
                        elif action == 'cancel':
                            if not request.is_request:
                                error="Invalid request type: Cannot cancel an invitation"
                            else:
                                request.end_date=now()
                                request.save()
                    elif request.team.creator==participant.user:
                        if action == 'accept':
                            if request.is_invitation:
                                error="Invalid request type: Cannot accept your own invitation"
                            else:
                                request.is_accepted=True
                                request.save()
                        elif action == 'reject':
                            if request.is_invitation:
                                error="Invalid request type: Cannot reject your own invitation"
                            else:
                                request.end_date=now()
                                request.save()
                        elif action == 'cancel':
                            if request.is_request:
                                error="Invalid request type: Cannot cancel a request"
                            else:
                                request.end_date=now()
                                request.save()
                    else:
                        error = "You cannot modify this request"
                else:
                    error = "Invalid request: This request is not active"
                context=super(RequestTeamView, self).get_context_data(**kwargs)
                context['action']=action;

                if error is not None:
                    context['error'] = error

        return context;

class NewRequestTeamView(LoginRequiredMixin, CreateView):
    model = TeamMembership
    template_name = "teams/request.html"
    form_class = forms.TeamMembershipForm

    def get_success_url(self):
        competition=Competition.objects.get(pk=self.kwargs['competition_pk']);
        return reverse('team_detail', kwargs={'competition_pk': competition.pk})
    def get_context_data(self, **kwargs):
        context = super(NewRequestTeamView, self).get_context_data(**kwargs)
        context['competition'] = Competition.objects.get(pk=self.kwargs['competition_pk'])
        context['team'] = Team.objects.get(pk=self.kwargs['team_pk'])
        return context
    def form_valid(self, form):
        form.instance.user=self.request.user
        form.instance.team=Team.objects.get(pk=self.kwargs['team_pk'])
        form.instance.start_date=now()
        form.instance.is_request=True
        form.save()
        return super(NewRequestTeamView, self).form_valid(form)

class TeamCreateView(LoginRequiredMixin, CreateView):
    model = Team
    template_name = "teams/edit.html"
    form_class = forms.TeamEditForm

    def get_success_url(self):
        return reverse('team_edit', kwargs={'competition_pk': self.object.competition.pk,'team_pk':self.object.pk})
    def get_context_data(self, **kwargs):
        context = super(TeamCreateView, self).get_context_data(**kwargs)
        context['competition'] = Competition.objects.get(pk=self.kwargs['competition_pk'])
        return context
    def form_valid(self, form):
        form.instance.creator=self.request.user
        form.instance.created_at=now()
        form.instance.competition=Competition.objects.get(pk=self.kwargs['competition_pk'])
        if form.instance.competition.require_team_approval:
            form.instance.status = TeamStatus.objects.get(codename=TeamStatus.PENDING)
        else:
            form.instance.status = TeamStatus.objects.get(codename=TeamStatus.APPROVED)
        #form.instance.image = form.cleaned_data['image']
        #form.image.save("revsys-logo.png", django_file, save=True)
        form.save()
        return super(TeamCreateView, self).form_valid(form)

class TeamEditView(LoginRequiredMixin, UpdateView):
    model = Team
    template_name = "teams/edit.html"
    form_class = forms.TeamEditForm
    pk_url_kwarg = 'team_pk'

    def get_success_url(self):
        return ''
    def get_context_data(self, **kwargs):
        context = super(TeamEditView, self).get_context_data(**kwargs)
        context['competition'] = Competition.objects.get(pk=self.kwargs['competition_pk'])
        context['information'] = {
            'Team name': self.object.name,
            'Description' : self.object.description,
            'Allow Requests': self.object.allow_requests,
            'Image': self.object.image
        }

        return context
