from django import forms
from django.core.validators import FileExtensionValidator

class UploadEpubForm(forms.Form):
    epub_file = forms.FileField(
        label="EPUB",
        validators=[FileExtensionValidator(allowed_extensions=["epub"])],
    )