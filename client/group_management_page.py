# Retired. This page used to manage OS groups on the *controller's own
# machine* (via backend/routes/groups.py's local grp/groupadd/groupdel
# calls) - inconsistent with the rest of the app, which targets enrolled
# remote hosts. Group management now lives in User Administration
# (client/user_administration_page.py: Create Group / Delete Group,
# dispatched to checked hosts via api.cmd_create_group/cmd_delete_group).
#
# Nothing imports this module anymore. Safe to delete this file.
