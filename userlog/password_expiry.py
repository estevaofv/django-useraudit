"""
Django password and account expiry.

This will prevent users from logging in unless they have changed their
password within a configurable password expiry period.

Expired users can reset their password using the normal registration
forms.

It will disable unused accounts. If users haven't logged in for a
certain time period, their account will be disabled next time a login
is attemped.

Requirement for password expiry: a custom auth user model is used.

Requirement for account expiry: whichever user model is used should
implement AbstractBaseUser (standard Django user model does of
course).


How to use:

1. Add "userlog" to the list of INSTALLED_APPS.

2. Put expiry backend *first* in the list of auth backends::

       AUTHENTICATION_BACKENDS = (
           'userlog.password_expiry.AccountExpiryBackend',
           # ... the rest ...
       )

3. Use a django custom auth model for your users and add a field for
   password expiry::

       # settings.py
       AUTH_USER_MODEL = "myapp.MyUser"
       AUTH_USER_MODEL_PASSWORD_CHANGE_DATE_ATTR = "password_change_date"

       # models.py
       from django.contrib.auth.models import AbstractUser

       class MyUser(AbstractUser):
           password_change_date = models.DateTimeField(
               auto_now_add=True,
               null=True,
           )

4. Configure the settings relevant to password expiry::

       # How long a user's password is good for. None or 0 means no expiration.
       PASSWORD_EXPIRY_DAYS = 180
       # How long before expiry will the frontend start bothering the user
       PASSWORD_EXPIRY_WARNING_DAYS = 30
       # # Disable the user's account if they haven't logged in for this time
       # ACCOUNT_EXPIRY_DAYS = 100

5. Add log handlers for "django.security" if they aren't already there.

6. Inspect all non-standard login views and make sure they are
   checking for User.is_active.

7. Add code to your frontend to nag the user if their password is due
   to expire. Otherwise one day they will be unable to login and they
   won't know why.

   todo: add an automatic process for e-mailing users before password expiry

8. In your deployment scripts, include a daily cronjob to run the
   disable_inactive_users management command. This will let users know
   if their account has been disabled. It requires the Sites framework
   to be enabled, and for the user model to have an "email" attribute.
"""

from collections import namedtuple
from datetime import timedelta
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db.models.signals import pre_save
from django.dispatch import receiver, Signal
from django.utils import timezone
import logging

logger = logging.getLogger("django.security")

__all__ = ["AccountExpiryBackend",
           "password_has_expired", "account_has_expired"]

password_has_expired = Signal(providing_args=["user"])
account_has_expired = Signal(providing_args=["user"])


@receiver(pre_save, sender=settings.AUTH_USER_MODEL)
def set_password_changed(sender, instance=None, created=False, raw=False, **kwargs):
    attrs = ExpirySettings.get()
    if not raw and attrs.date_changed:
        update_date_changed(instance, attrs)


def update_date_changed(user, attrs):
    if user.pk is not None:
        # do another query to get user's previous password
        old_user = type(user).objects.get(pk=user.pk)
        old_password = getattr(old_user, attrs.password)
    else:
        # user is not created yet
        old_password = None

    if old_password != user.password:
        setattr(user, attrs.date_changed, timezone.now())


def is_password_expired(user):
    earliest = ExpirySettings.get().earliest_possible_password_change
    if earliest:
        change_date = get_password_change_date(user)
        return change_date and change_date < earliest
    return False


def get_password_change_date(user):
    attr = ExpirySettings.get().date_changed
    if attr:
        if hasattr(user, attr):
            return getattr(user, attr)
        else:
            logger.warning("User model does not have a %s attribute" % attr)
    return None


def get_user_last_login(user):
    if hasattr(user, "last_login"):
        return user.last_login
    else:
        logger.warning("User model doesn't have last_login field. ACCOUNT_EXPIRY_DAYS setting will have no effect.")
        return None


def is_account_expired(user):
    earliest = ExpirySettings.get().earliest_possible_login
    if earliest:
        last_login = get_user_last_login(user)
        return last_login and last_login < earliest
    return False


class ExpirySettings(namedtuple("ExpirySettings", ["num_days", "num_warning_days", "date_changed", "password", "account_expiry"])):
    @classmethod
    def get(cls):
        expiry = getattr(settings, "PASSWORD_EXPIRY_DAYS", None) or 0
        warning = getattr(settings, "PASSWORD_EXPIRY_WARNING_DAYS", None) or 0
        date_changed = getattr(settings, "AUTH_USER_MODEL_PASSWORD_CHANGE_DATE_ATTR", None) or None
        password = getattr(settings, "AUTH_USER_MODEL_PASSWORD_ATTR", None) or "password"
        account_expiry = getattr(settings, "ACCOUNT_EXPIRY_DAYS", None) or 0
        return cls(expiry, warning, date_changed, password, account_expiry)

    @property
    def earliest_possible_login(self):
        if self.account_expiry > 0:
            return timezone.now() - timedelta(days=self.account_expiry)
        return None

    @property
    def earliest_possible_password_change(self):
        if self.num_days > 0:
            return timezone.now() - timedelta(days=self.num_days)
        return None


class AccountExpiryBackend(object):
    """
    This backend doesn't authenticate, it just prevents authentication
    of a user whose account password has expired.
    """
    def authenticate(self, username=None, password=None, **kwargs):
        user = self._lookup_user(username, password, **kwargs)

        if user:
            # Prevent authentication of inactive users (if the user
            # model supports it). Django only checks is_active at the
            # login view level.
            if hasattr(user, "is_active") and not user.is_active:
                raise PermissionDenied("Account is not active")

            if is_password_expired(user):
                logger.info("User's password has expired: %s" % user)
                password_has_expired.send(sender=user.__class__, user=user)
                raise PermissionDenied("Password has expired")

            if  is_account_expired(user):
                logger.info("Disabling stale user account: %s" % user)
                user.is_active = False
                user.save()
                account_has_expired.send(sender=user.__class__, user=user)
                raise PermissionDenied("Account has expired")

        # pass on to next handler
        return None

    def _lookup_user(self, username=None, password=None, **kwargs):
        # This is the same procedure as in
        # django.contrib.auth.backends.ModelBackend, except without
        # the timing attack mitigation, because it doesn't take long
        # to check for expiry.
        UserModel = get_user_model()
        if username is None:
            username = kwargs.get(UserModel.USERNAME_FIELD)
        try:
            return UserModel._default_manager.get_by_natural_key(username)
        except UserModel.DoesNotExist:
            return None
