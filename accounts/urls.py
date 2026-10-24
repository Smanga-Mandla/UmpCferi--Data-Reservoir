from django.urls import path

from accounts.views import (
    EmailLoginView,
    UserLogoutView,
    UserPasswordResetCompleteView,
    UserPasswordResetConfirmView,
    UserPasswordResetDoneView,
    UserPasswordResetView,
    deactivate_user,
    edit_user_access,
    invite_user,
    manage_users,
    mfa_setup,
    mfa_verify,
    role_matrix,
)

app_name = "accounts"

urlpatterns = [
    path("login/", EmailLoginView.as_view(), name="login"),
    path("logout/", UserLogoutView.as_view(), name="logout"),
    path("password-reset/", UserPasswordResetView.as_view(), name="password_reset"),
    path("password-reset/done/", UserPasswordResetDoneView.as_view(), name="password_reset_done"),
    path(
        "password-reset/<uidb64>/<token>/",
        UserPasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path("password-reset/complete/", UserPasswordResetCompleteView.as_view(), name="password_reset_complete"),
    path("mfa/setup/", mfa_setup, name="mfa_setup"),
    path("mfa/verify/", mfa_verify, name="mfa_verify"),
    path("manage/", manage_users, name="manage_users"),
    path("manage/invite/", invite_user, name="invite_user"),
    path("manage/<int:user_id>/access/", edit_user_access, name="edit_user_access"),
    path("manage/<int:user_id>/deactivate/", deactivate_user, name="deactivate_user"),
    path("role-matrix/", role_matrix, name="role_matrix"),
]
