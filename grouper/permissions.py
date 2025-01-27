import re
from collections import defaultdict, namedtuple
from datetime import datetime
from typing import TYPE_CHECKING

from six import iteritems, itervalues
from sqlalchemy import asc
from sqlalchemy.exc import IntegrityError

from grouper.audit import assert_controllers_are_auditors
from grouper.constants import ARGUMENT_VALIDATION, PERMISSION_ADMIN, PERMISSION_GRANT
from grouper.email_util import EmailTemplateEngine, send_email
from grouper.models.audit_log import AuditLog
from grouper.models.base.constants import OBJ_TYPES_IDX
from grouper.models.comment import Comment
from grouper.models.counter import Counter
from grouper.models.group import Group
from grouper.models.permission import Permission
from grouper.models.permission_map import PermissionMap
from grouper.models.permission_request import PermissionRequest
from grouper.models.permission_request_status_change import PermissionRequestStatusChange
from grouper.models.service_account_permission_map import ServiceAccountPermissionMap
from grouper.plugin import get_plugin_proxy
from grouper.settings import settings
from grouper.user_group import get_groups_by_user
from grouper.util import matches_glob

if TYPE_CHECKING:
    from grouper.models.base.session import Session
    from grouper.models.user import User
    from typing import Dict, List, Optional, Set, Tuple

# Singleton
GLOBAL_OWNERS = object()

# represents all information we care about for a list of permission requests
Requests = namedtuple(
    "Requests", ["requests", "status_change_by_request_id", "comment_by_status_change_id"]
)


# represents a permission grant, essentially what come back from User.my_permissions()
# TODO: consider replacing that output with this namedtuple
Grant = namedtuple("Grant", "name, argument")


class NoSuchPermission(Exception):
    """No permission by this name exists."""

    def __init__(self, name):
        # type: (str) -> None
        self.name = name


class CannotDisableASystemPermission(Exception):
    """Cannot disable key system permissions."""

    def __init__(self, name):
        # type: (str) -> None
        """
        Arg(s):
            name(str): name of the permission being disabled
        """
        self.name = name


def create_permission(session, name, description=""):
    # type: (Session, str, Optional[str]) -> Permission
    """Create and add a new permission to database

    Arg(s):
        session(models.base.session.Session): database session
        name(str): the name of the permission
        description(str): the description of the permission

    Returns:
        The created permission that has been added to the session
    """
    permission = Permission(name=name, description=description or "")
    permission.add(session)
    return permission


def get_all_permissions(session, include_disabled=False):
    # type: (Session, Optional[bool]) -> List[Permission]
    """Get permissions that exist in the database, either only enabled
    permissions, or both enabled and disabled ones

    Arg(s):
        session(models.base.session.Session): database session
        include_disabled(bool): True to also include disabled
            permissions. Make sure you really want this.

    Returns:
        List of permissions
    """
    query = session.query(Permission)
    if not include_disabled:
        query = query.filter(Permission.enabled == True)
    return query.order_by(asc(Permission.name)).all()


def get_permission(session, name):
    # type: (Session, str) -> Optional[Permission]
    """Get a permission

    Arg(s):
        session(models.base.session.Session): database session
        name(str): the name of the permission

    Returns:
        The permission if found, None otherwise
    """
    return Permission.get(session, name=name)


def get_or_create_permission(session, name, description=""):
    # type: (Session, str, Optional[str]) -> Tuple[Optional[Permission], bool]
    """Get a permission or create it if it doesn't already exist

    Arg(s):
        session(models.base.session.Session): database session
        name(str): the name of the permission
        description(str): the description for the permission if it is created

    Returns:
        (permission, is_new) tuple
    """
    perm = get_permission(session, name)
    is_new = False
    if not perm:
        is_new = True
        perm = create_permission(session, name, description=description or "")
    return perm, is_new


def grant_permission(session, group_id, permission_id, argument=""):
    """
    Grant a permission to this group. This will fail if the (permission, argument) has already
    been granted to this group.

    Args:
        session(models.base.session.Session): database session
        permission(Permission): a Permission object being granted
        argument(str): must match constants.ARGUMENT_VALIDATION

    Throws:
        AssertError if argument does not match ARGUMENT_VALIDATION regex
    """
    assert re.match(
        ARGUMENT_VALIDATION + r"$", argument
    ), "Permission argument does not match regex."

    mapping = PermissionMap(permission_id=permission_id, group_id=group_id, argument=argument)
    mapping.add(session)

    Counter.incr(session, "updates")

    session.commit()


def grant_permission_to_service_account(session, account, permission, argument=""):
    """
    Grant a permission to this service account. This will fail if the (permission, argument) has
    already been granted to this group.

    Args:
        session(models.base.session.Session): database session
        account(ServiceAccount): a ServiceAccount object being granted a permission
        permission(Permission): a Permission object being granted
        argument(str): must match constants.ARGUMENT_VALIDATION

    Throws:
        AssertError if argument does not match ARGUMENT_VALIDATION regex
    """
    assert re.match(
        ARGUMENT_VALIDATION + r"$", argument
    ), "Permission argument does not match regex."

    mapping = ServiceAccountPermissionMap(
        permission_id=permission.id, service_account_id=account.id, argument=argument
    )
    mapping.add(session)

    Counter.incr(session, "updates")

    session.commit()


def enable_permission_auditing(session, permission_name, actor_user_id):
    """Set a permission as audited.

    Args:
        session(models.base.session.Session): database session
        permission_name(str): name of permission in question
        actor_user_id(int): id of user who is enabling auditing
    """
    permission = get_permission(session, permission_name)
    if not permission:
        raise NoSuchPermission(name=permission_name)

    permission.audited = True

    AuditLog.log(
        session,
        actor_user_id,
        "enable_auditing",
        "Enabled auditing.",
        on_permission_id=permission.id,
    )

    Counter.incr(session, "updates")

    session.commit()


def disable_permission_auditing(session, permission_name, actor_user_id):
    """Set a permission as audited.

    Args:
        session(models.base.session.Session): database session
        permission_name(str): name of permission in question
        actor_user_id(int): id of user who is disabling auditing
    """
    permission = get_permission(session, permission_name)
    if not permission:
        raise NoSuchPermission(name=permission_name)

    permission.audited = False

    AuditLog.log(
        session,
        actor_user_id,
        "disable_auditing",
        "Disabled auditing.",
        on_permission_id=permission.id,
    )

    Counter.incr(session, "updates")

    session.commit()


def get_groups_by_permission(session, permission):
    """For an enabled permission, return the groups and associated arguments that
    have that permission. If the permission is disabled, return empty list.

    Args:
        session(models.base.session.Session): database session
        permission(models.Permission): permission in question

    Returns:
        List of 2-tuple of the form (Group, argument).
    """
    if not permission.enabled:
        return []
    return (
        session.query(Group.groupname, PermissionMap.argument, PermissionMap.granted_on)
        .filter(
            Group.id == PermissionMap.group_id,
            PermissionMap.permission_id == permission.id,
            Group.enabled == True,
        )
        .all()
    )


def get_log_entries_by_permission(session, permission, limit=20):
    """For a given permission, return the audit logs that pertain.

    Args:
        session(models.base.session.Session): database session
        permission_name(Permission): permission in question
        limit(int): number of results to return
    """
    return AuditLog.get_entries(session, on_permission_id=permission.id, limit=limit)


def filter_grantable_permissions(session, grants, all_permissions=None):
    """For a given set of PERMISSION_GRANT permissions, return all enabled
    permissions that are grantable.

    Args:
        session (sqlalchemy.orm.session.Session); database session
        grants ([Permission, ...]): PERMISSION_GRANT permissions
        all_permissions ({name: Permission}): all permissions to check against

    Returns:
        list of (Permission, argument) that is grantable by list of grants
        sorted by permission name and argument.
    """

    if all_permissions is None:
        all_permissions = {
            permission.name: permission for permission in get_all_permissions(session)
        }

    result = []
    for grant in grants:
        assert grant.name == PERMISSION_GRANT

        grantable = grant.argument.split("/", 1)
        if not grantable:
            continue
        for name, permission_obj in iteritems(all_permissions):
            if matches_glob(grantable[0], name):
                result.append((permission_obj, grantable[1] if len(grantable) > 1 else "*"))

    return sorted(result, key=lambda x: x[0].name + x[1])


def get_owners_by_grantable_permission(session, separate_global=False):
    """
    Returns all known permission arguments with owners. This consolidates
    permission grants supported by grouper itself as well as any grants
    governed by plugins.

    Args:
        session(sqlalchemy.orm.session.Session): database session
        separate_global(bool): Whether or not to construct a specific entry for
                               GLOBAL_OWNER in the output map

    Returns:
        A map of permission to argument to owners of the form {permission:
        {argument: [owner1, ...], }, } where 'owners' are models.Group objects.
        And 'argument' can be '*' which means 'anything'.
    """
    all_permissions = {permission.name: permission for permission in get_all_permissions(session)}
    all_groups = session.query(Group).filter(Group.enabled == True).all()

    owners_by_arg_by_perm = defaultdict(lambda: defaultdict(list))

    all_group_permissions = (
        session.query(Permission.name, PermissionMap.argument, PermissionMap.granted_on, Group)
        .filter(PermissionMap.group_id == Group.id, Permission.id == PermissionMap.permission_id)
        .all()
    )

    grants_by_group = defaultdict(list)

    for grant in all_group_permissions:
        grants_by_group[grant.Group.id].append(grant)

    for group in all_groups:
        # special case permission admins
        group_permissions = grants_by_group[group.id]
        if any([g.name == PERMISSION_ADMIN for g in group_permissions]):
            for perm_name in all_permissions:
                owners_by_arg_by_perm[perm_name]["*"].append(group)
            if separate_global:
                owners_by_arg_by_perm[GLOBAL_OWNERS]["*"].append(group)
            continue

        grants = [gp for gp in group_permissions if gp.name == PERMISSION_GRANT]

        for perm, arg in filter_grantable_permissions(
            session, grants, all_permissions=all_permissions
        ):
            owners_by_arg_by_perm[perm.name][arg].append(group)

    # merge in plugin results
    for res in get_plugin_proxy().get_owner_by_arg_by_perm(session):
        for perm, owners_by_arg in iteritems(res):
            for arg, owners in iteritems(owners_by_arg):
                owners_by_arg_by_perm[perm][arg] += owners

    return owners_by_arg_by_perm


def get_grantable_permissions(session, restricted_ownership_permissions):
    """Returns all grantable permissions and their possible arguments. This
    function attempts to reduce a permission's arguments to the least
    permissive possible.

    Args:
        session(sqlalchemy.orm.session.Session): database session
        restricted_ownership_permissions(List[str]): list of permissions for which
            we exclude wildcard ownership from the result if any non-wildcard
            owners exist

    Returns:
        A map of models.Permission object to a list of possible arguments, i.e.
        {models.Permission: [arg1, arg2, ...], ...}
    """
    owners_by_arg_by_perm = get_owners_by_grantable_permission(session)
    args_by_perm = defaultdict(list)
    for permission, owners_by_arg in iteritems(owners_by_arg_by_perm):
        for argument in owners_by_arg:
            args_by_perm[permission].append(argument)

    def _reduce_args(perm_name, args):
        non_wildcard_args = [a != "*" for a in args]
        if (
            restricted_ownership_permissions
            and perm_name in restricted_ownership_permissions
            and any(non_wildcard_args)
        ):
            # at least one none wildcard arg so we only return those and we care
            return sorted({a for a in args if a != "*"})
        elif all(non_wildcard_args):
            return sorted(set(args))
        else:
            # it's all wildcard so return that one
            return ["*"]

    return {p: _reduce_args(p, a) for p, a in iteritems(args_by_perm)}


def get_owner_arg_list(session, permission, argument, owners_by_arg_by_perm=None):
    """Return the grouper group(s) responsible for approving a request for the
    given permission + argument along with the actual argument they were
    granted.

    Args:
        session(sqlalchemy.orm.session.Session): database session
        permission(models.Permission): permission in question
        argument(str): argument for the permission
        owners_by_arg_by_perm(Dict): list of groups that can grant a given
            permission, argument pair in the format of
            {perm_name: {argument: [group1, group2, ...], ...}, ...}
            This is for convenience/caching if the value has already been fetched.
    Returns:
        list of 2-tuple of (group, argument) where group is the models.Group
        grouper groups responsibile for permimssion+argument and argument is
        the argument actually granted to that group. can be empty.
    """
    if owners_by_arg_by_perm is None:
        owners_by_arg_by_perm = get_owners_by_grantable_permission(session)

    all_owner_arg_list = []
    owners_by_arg = owners_by_arg_by_perm[permission.name]
    for arg, owners in iteritems(owners_by_arg):
        if matches_glob(arg, argument):
            all_owner_arg_list += [(owner, arg) for owner in owners]

    return all_owner_arg_list


class PermissionRequestException(Exception):
    pass


class RequestAlreadyExists(PermissionRequestException):
    """Trying to create a request for a permission + argument + group which
    already exists in "pending" state."""


class NoOwnersAvailable(PermissionRequestException):
    """No owner was found for the permission + argument combination."""


class RequestAlreadyGranted(PermissionRequestException):
    """Group already has requested permission + argument pair."""


# hmm maybe don't need this. people can do things to the permission,
# e.g., grant it to groups, request grants for it, revoke it from
# groups, etc. and we kind of don't care, as long as the permission's
# disabled state prevents it from being used, which is the important
# bit
class PermissionIsDisabled(PermissionRequestException):
    """Trying to operate on a permission that is disabled."""


def create_request(session, user, group, permission, argument, reason):
    # type: (Session, User, Group, Permission, str, str) -> PermissionRequest
    """
    Creates an permission request and sends notification to the responsible approvers.

    Args:
        session(sqlalchemy.orm.session.Session): database session
        user(models.User): user requesting permission
        group(models.Group): group requested permission would be applied to
        permission(models.Permission): permission in question to request
        argument(str): argument for the given permission
        reason(str): reason the permission should be granted

    Raises:
        RequestAlreadyExists if trying to create a request that is already pending
        NoOwnersAvailable if no owner is available for the requested perm + arg.
        grouper.audit.UserNotAuditor if the group has owners that are not auditors
    """
    # check if group already has perm + arg pair
    for _, existing_perm_name, _, existing_perm_argument, _ in group.my_permissions():
        if permission.name == existing_perm_name and argument == existing_perm_argument:
            raise RequestAlreadyGranted()

    # check if request already pending for this perm + arg pair
    existing_count = (
        session.query(PermissionRequest)
        .filter(
            PermissionRequest.group_id == group.id,
            PermissionRequest.permission_id == permission.id,
            PermissionRequest.argument == argument,
            PermissionRequest.status == "pending",
        )
        .count()
    )

    if existing_count > 0:
        raise RequestAlreadyExists()

    # determine owner(s)
    owners_by_arg_by_perm = get_owners_by_grantable_permission(session, separate_global=True)
    owner_arg_list = get_owner_arg_list(
        session, permission, argument, owners_by_arg_by_perm=owners_by_arg_by_perm
    )

    if not owner_arg_list:
        raise NoOwnersAvailable()

    if permission.audited:
        # will raise UserNotAuditor if any owner of the group is not an auditor
        assert_controllers_are_auditors(group)

    pending_status = "pending"
    now = datetime.utcnow()

    # multiple steps to create the request
    request = PermissionRequest(
        requester_id=user.id,
        group_id=group.id,
        permission_id=permission.id,
        argument=argument,
        status=pending_status,
        requested_at=now,
    ).add(session)
    session.flush()

    request_status_change = PermissionRequestStatusChange(
        request=request, user=user, to_status=pending_status, change_at=now
    ).add(session)
    session.flush()

    Comment(
        obj_type=OBJ_TYPES_IDX.index("PermissionRequestStatusChange"),
        obj_pk=request_status_change.id,
        user_id=user.id,
        comment=reason,
        created_on=now,
    ).add(session)

    # send notification
    email_context = {
        "user_name": user.name,
        "group_name": group.name,
        "permission_name": permission.name,
        "argument": argument,
        "reason": reason,
        "request_id": request.id,
        "references_header": request.reference_id,
    }

    # TODO: would be nicer if it told you which group you're an approver of
    # that's causing this notification

    mail_to = []
    global_owners = owners_by_arg_by_perm[GLOBAL_OWNERS]["*"]
    non_wildcard_owners = [grant for grant in owner_arg_list if grant[1] != "*"]
    non_global_owners = [grant for grant in owner_arg_list if grant[0] not in global_owners]
    if any(non_wildcard_owners):
        # non-wildcard owners should get all the notifications
        mailto_owner_arg_list = non_wildcard_owners
    elif any(non_global_owners):
        mailto_owner_arg_list = non_global_owners
    else:
        # only the wildcards so they get the notifications
        mailto_owner_arg_list = owner_arg_list

    for owner, arg in mailto_owner_arg_list:
        if owner.email_address:
            mail_to.append(owner.email_address)
        else:
            mail_to.extend([u for t, u in owner.my_members() if t == "User"])

    template_engine = EmailTemplateEngine(settings())
    subject_template = template_engine.get_template("email/pending_permission_request_subj.tmpl")
    subject = subject_template.render(permission=permission.name, group=group.name)
    send_email(
        session, set(mail_to), subject, "pending_permission_request", settings(), email_context
    )

    return request


def get_pending_request_by_group(session, group):
    """Load pending request for a particular group.

    Args:
        session(sqlalchemy.orm.session.Session): database session
        group(models.Group): group in question

    Returns:
        list of models.PermissionRequest correspodning to open/pending requests
        for this group.
    """
    return (
        session.query(PermissionRequest)
        .filter(PermissionRequest.status == "pending", PermissionRequest.group_id == group.id)
        .all()
    )


def can_approve_request(session, request, owner, group_ids=None, owners_by_arg_by_perm=None):
    owner_arg_list = get_owner_arg_list(
        session, request.permission, request.argument, owners_by_arg_by_perm
    )
    if group_ids is None:
        group_ids = {g.id for g, _ in get_groups_by_user(session, owner)}

    return group_ids.intersection([o.id for o, arg in owner_arg_list])


def get_requests(
    session, status, limit, offset, owner=None, requester=None, owners_by_arg_by_perm=None
):
    """Load requests using the given filters.

    Args:
        session(sqlalchemy.orm.session.Session): database session
        status(models.base.constants.REQUEST_STATUS_CHOICES): if not None,
                filter by particular status
        limit(int): how many results to return
        offset(int): the offset into the result set that should be applied
        owner(models.User): if not None, filter by requests that the owner
            can action
        requester(models.User): if not None, filter by requests that the
            requester made
        owners_by_arg_by_perm(Dict): list of groups that can grant a given
            permission, argument pair in the format of
            {perm_name: {argument: [group1, group2, ...], ...}, ...}
            This is for convenience/caching if the value has already been fetched.

    Returns:
        2-tuple of (Requests, total) where total is total result size and
        Requests is the namedtuple with requests and associated
        comments/changes.
    """
    # get all requests
    all_requests = session.query(PermissionRequest)
    if status:
        all_requests = all_requests.filter(PermissionRequest.status == status)
    if requester:
        all_requests = all_requests.filter(PermissionRequest.requester_id == requester.id)

    all_requests = all_requests.order_by(PermissionRequest.requested_at.desc()).all()

    if owners_by_arg_by_perm is None:
        owners_by_arg_by_perm = get_owners_by_grantable_permission(session)

    if owner:
        group_ids = {g.id for g, _ in get_groups_by_user(session, owner)}
        requests = [
            request
            for request in all_requests
            if can_approve_request(
                session,
                request,
                owner,
                group_ids=group_ids,
                owners_by_arg_by_perm=owners_by_arg_by_perm,
            )
        ]
    else:
        requests = all_requests

    total = len(requests)
    requests = requests[offset:limit]

    status_change_by_request_id = defaultdict(list)
    if not requests:
        comment_by_status_change_id = {}
    else:
        status_changes = (
            session.query(PermissionRequestStatusChange)
            .filter(PermissionRequestStatusChange.request_id.in_([r.id for r in requests]))
            .all()
        )
        for sc in status_changes:
            status_change_by_request_id[sc.request_id].append(sc)

        comments = (
            session.query(Comment)
            .filter(
                Comment.obj_type == OBJ_TYPES_IDX.index("PermissionRequestStatusChange"),
                Comment.obj_pk.in_([s.id for s in status_changes]),
            )
            .all()
        )
        comment_by_status_change_id = {c.obj_pk: c for c in comments}

    return (Requests(requests, status_change_by_request_id, comment_by_status_change_id), total)


def get_request_by_id(session, request_id):
    """Get a single request by the request ID.

    Args:
        session(sqlalchemy.orm.session.Session): database session
        request_id(int): id of request in question

    Returns:
        model.PermissionRequest object or None if no request by that ID.
    """
    return session.query(PermissionRequest).filter(PermissionRequest.id == request_id).one()


def get_changes_by_request_id(session, request_id):
    status_changes = (
        session.query(PermissionRequestStatusChange)
        .filter(PermissionRequestStatusChange.request_id == request_id)
        .all()
    )

    comments = (
        session.query(Comment)
        .filter(
            Comment.obj_type == OBJ_TYPES_IDX.index("PermissionRequestStatusChange"),
            Comment.obj_pk.in_([s.id for s in status_changes]),
        )
        .all()
    )
    comment_by_status_change_id = {c.obj_pk: c for c in comments}

    return [(sc, comment_by_status_change_id[sc.id]) for sc in status_changes]


def update_request(
    session,  # type: Session
    request,  # type: PermissionRequest
    user,  # type: User
    new_status,  # type: str
    comment,  # type: str
):
    # type: (...) -> None
    """Update a request.

    Args:
        session(sqlalchemy.orm.session.Session): database session
        request(models.PermissionRequest): request to update
        user(models.User): user making update
        new_status(models.base.constants.REQUEST_STATUS_CHOICES): new status
        comment(str): comment to include with status change

    Raises:
        grouper.audit.UserNotAuditor in case we're trying to add an audited
            permission to a group without auditors
    """
    if request.status == new_status:
        # nothing to do
        return

    # make sure the grant can happen
    if new_status == "actioned":
        if request.permission.audited:
            # will raise UserNotAuditor if no auditors are owners of the group
            assert_controllers_are_auditors(request.group)

    # all rows we add have the same timestamp
    now = datetime.utcnow()

    # new status change row
    permission_status_change = PermissionRequestStatusChange(
        request=request,
        user_id=user.id,
        from_status=request.status,
        to_status=new_status,
        change_at=now,
    ).add(session)
    session.flush()

    # new comment
    Comment(
        obj_type=OBJ_TYPES_IDX.index("PermissionRequestStatusChange"),
        obj_pk=permission_status_change.id,
        user_id=user.id,
        comment=comment,
        created_on=now,
    ).add(session)

    # update permissionRequest status
    request.status = new_status
    session.commit()

    if new_status == "actioned":
        # actually grant permission
        try:
            grant_permission(session, request.group.id, request.permission.id, request.argument)
        except IntegrityError:
            session.rollback()

    # audit log
    AuditLog.log(
        session,
        user.id,
        "update_perm_request",
        "updated permission request to status: {}".format(new_status),
        on_group_id=request.group_id,
        on_user_id=request.requester_id,
        on_permission_id=request.permission.id,
    )

    session.commit()

    # send notification

    template_engine = EmailTemplateEngine(settings())
    subject_template = template_engine.get_template("email/pending_permission_request_subj.tmpl")
    subject = "Re: " + subject_template.render(
        permission=request.permission.name, group=request.group.name
    )

    if new_status == "actioned":
        email_template = "permission_request_actioned"
    else:
        email_template = "permission_request_cancelled"

    email_context = {
        "group_name": request.group.name,
        "action_taken_by": user.name,
        "reason": comment,
        "permission_name": request.permission.name,
        "argument": request.argument,
    }

    send_email(
        session, [request.requester.name], subject, email_template, settings(), email_context
    )


def permission_list_to_dict(perms):
    # type: (List[Permission]) -> Dict[str, Dict[str, Permission]]
    """Converts a list of Permission objects into a dictionary indexed by the permission names.
    That dictionary in turn holds another dictionary which is indexed by permission argument, and
    stores the Permission object

    Args:
        perms: a list containing Permission objects

    Returns:
        a dictionary with the permission names as keys, and has as values another dictionary
        with permission arguments as keys and Permission objects as values
    """
    ret = defaultdict(dict)  # type: Dict[str, Dict[str, Permission]]
    for perm in perms:
        ret[perm.name][perm.argument] = perm
    return ret


def permission_intersection(perms_a, perms_b):
    # type: (List[Permission], List[Permission]) -> Set[Permission]
    """Returns the intersection of the two Permission lists, taking into account the special
    handling of argument wildcards

    Args:
        perms_a: the first list of permissions
        perms_b: the second list of permissions

    Returns:
        a set of all permissions that both perms_a and perms_b grant access to
    """
    pdict_b = permission_list_to_dict(perms_b)
    ret = set()
    for perm in perms_a:
        if perm.name not in pdict_b:
            continue
        if perm.argument in pdict_b[perm.name]:
            ret.add(perm)
            continue
        # Unargumented permissions are granted by any permission with the same name
        if perm.argument == "":
            ret.add(perm)
            continue
        # Argument wildcard
        if "*" in pdict_b[perm.name]:
            ret.add(perm)
            continue
        # Unargumented permissions are granted by any permission with the same name
        if "" in pdict_b[perm.name]:
            ret.add(pdict_b[perm.name][""])
            continue
        # If this permission is a wildcard, we add all permissions with the same name from
        # the other set
        if perm.argument == "*":
            ret |= {p for p in itervalues(pdict_b[perm.name])}
    return ret
