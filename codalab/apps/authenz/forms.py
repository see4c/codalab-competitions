from django import forms


class CodalabSignupForm(forms.Form):
    image = forms.ImageField(required=False)
    team_name = forms.CharField(max_length=64, required=False)
    contact_email = forms.EmailField(required=False)
    method_name = forms.CharField(max_length=20, required=False)
    method_description = forms.CharField(required=False)
    project_url = forms.URLField(required=False)
    publication_url = forms.URLField(required=False)
    organization_or_affiliation = forms.CharField(max_length=255, required=False)
    bibtex = forms.CharField(required=False)
    biography = forms.CharField(required=False)
    webpage = forms.CharField(max_length=255, required=False)
    ORCID = forms.CharField(max_length=255, required=False)

    class Meta():
        widgets = {
            'team_members': forms.Textarea(attrs={"class": "form-control"}),
            'method_description': forms.Textarea(attrs={"class": "form-control"}),
            'bibtex': forms.Textarea(attrs={"class": "form-control"}),
            'biography': forms.Textarea(attrs={"class": "form-control"})
        }

    def save(self, new_user):
        new_user.__dict__.update({
            'organization_or_affiliation': self.cleaned_data['organization_or_affiliation'],
            'team_name': self.cleaned_data['team_name'],
            'method_name': self.cleaned_data['method_name'],
            'method_description': self.cleaned_data['method_description'],
            'contact_email': self.cleaned_data['contact_email'],
            'project_url': self.cleaned_data['project_url'],
            'publication_url': self.cleaned_data['publication_url'],
            'bibtex': self.cleaned_data['bibtex'],
            'biography': self.cleaned_data['biography'],
            'webpage': self.cleaned_data['webpage'],
            'ORCID': self.cleaned_data['ORCID'],
            'image': self.cleaned_data['image'],
        })
        new_user.save()

