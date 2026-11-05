"""Admin configuration for the custom user model."""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm

from accounts.models import User


class EmailNormalizationMixin:
    """Normalize admin-entered email addresses before validation."""

    def clean_email(self):
        """Return a trimmed, lowercase email address."""
        return self.cleaned_data["email"].strip().lower()


class EmailUserCreationForm(EmailNormalizationMixin, UserCreationForm):
    """Create users through the email-based admin form."""

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("email", "is_active", "is_staff", "is_superuser")


class EmailUserChangeForm(EmailNormalizationMixin, UserChangeForm):
    """Edit users through the email-based admin form."""

    class Meta(UserChangeForm.Meta):
        model = User
        fields = "__all__"


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """Manage users without exposing a username field."""

    form = EmailUserChangeForm
    add_form = EmailUserCreationForm
    ordering = ("email",)
    list_display = ("email", "is_staff", "is_active")
    search_fields = ("email",)
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name")}),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "email",
                    "password1",
                    "password2",
                    "is_active",
                    "is_staff",
                    "is_superuser",
                ),
            },
        ),
    )
    filter_horizontal = ("groups", "user_permissions")
