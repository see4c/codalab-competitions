from django.utils.timezone import now
from django import forms
from .models import Team
from tinymce.widgets import TinyMCE
from awesome_avatar import forms as avatar_forms


class TeamEditForm(forms.ModelForm):
    name = forms.CharField(max_length=64, required=False)
    description = forms.Textarea()
    allow_requests = forms.BooleanField(required=False)
    is_active = forms.CheckboxInput()
    image = avatar_forms.AvatarField()

    class Meta:
        model = Team
        fields = (
            'name',
            'description',
            'allow_requests',
            'is_active',
        )
        widgets = {
            'description' : TinyMCE(attrs={'class' : 'team-editor-description'},
                                    mce_attrs={"theme" : "advanced", "cleanup_on_startup" : True, "theme_advanced_toolbar_location" : "top", "gecko_spellcheck" : True, "width" : "100%"})
        }
    def clean_name(self):
        if 'name' in self.changed_data:
            if Team.objects.filter(name=self.cleaned_data['name'], competition_id=self.instance.competition_id).count()>0:
                raise forms.ValidationError("This name is already used by another team", code="duplicated_name")

        return self.cleaned_data["name"]
