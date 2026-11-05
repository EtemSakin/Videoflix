"""User manager for email-based authentication."""

from django.contrib.auth.base_user import BaseUserManager


class UserManager(BaseUserManager):
    """Create users with normalized email addresses and hashed passwords."""

    use_in_migrations = True

    def create_user(self, email, password=None, **extra_fields):
        """Create a user with a normalized email address."""
        if not email or not email.strip():
            raise ValueError("An email address is required.")
        user = self.model(email=email.strip().lower(), **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        """Create a user with staff and superuser privileges."""
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("A superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("A superuser must have is_superuser=True.")
        return self.create_user(email, password, **extra_fields)
