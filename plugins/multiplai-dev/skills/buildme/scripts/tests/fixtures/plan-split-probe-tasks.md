## 1. A cleaning company is an account from the moment it is created, and renames follow it

This block changes `cleaning_providers_api` POST in `backend/DolceCleaning/views.py`. The new `DolcePMS.ImpresaPulizie` and its cleaning-company account, named with the trimmed `nome`, are created in one `transaction.atomic()` through account-model's account-creation path, with no user. `cleaning_provider_detail_api` PUT changes too:
- a `nome` that is empty or only spaces is refused with 400 `Il campo 'nome' è obbligatorio`, and nothing is saved;
- otherwise the company and the account name are saved in the same transaction.

`views.py` never imports `DolceSystem.ImpresaPulizie` and never reads `Utente.impresa_pulizie`.

Tests in `backend/DolceCleaning/tests/test_dfaccmgmt10_utenti_impresa.py`:
- creating "Pulito Srl" gives an account named "Pulito Srl" with no users, and the company appears in `GET api/dolcecleaning/providers`;
- renaming it to "Pulito Group Srl" shows the new name on both the company and the account;
- a rename to `"   "` returns 400 and leaves both names unchanged;
- the helper `assert_legacy_impresa_rows_unchanged(action)` snapshots every `DolceSystem.ImpresaPulizie` row before and after create and rename and finds them identical. Later blocks pass their own actions to this helper.

Interfaces:
- Produces: `def _provider_account(provider_id: int) -> tuple[ImpresaPulizie, Account] | None` in `backend/DolceCleaning/views.py` (returns `None` when no company has that id; the view then returns `_json_error("Impresa non trovata", status=404)`)
- Produces: `def assert_legacy_impresa_rows_unchanged(action: Callable[[], object]) -> None` in `backend/DolceCleaning/tests/test_dfaccmgmt10_utenti_impresa.py`

Satisfies: dolcecleaning-company-account (A cleaning company is an account even with no users: Company created without users; Cleaning companies are created only in DolceCleaning: Company appears after creation in DolceCleaning; Company name is edited in DolceCleaning: Rename a company; Legacy cleaning companies are untouched: Legacy rows unchanged)

## 2. The Utenti tab lists every user with role, status and last access, for operators only

**Backend.** `cleaning_provider_users_api` declares `@permission_classes([IsAuthenticated, IsOperatore])` and resolves the account with `_provider_account`. It calls account-user-api's list operation with the account and `request.user`, and returns:

`{"ok": true, "items": [...], "restricted_actions_allowed": <bool>}`

Each item has these fields:
- `id`: `Utente.id`
- `user_id`
- `username`
- `first_name`
- `last_name`
- `email`
- `tel`
- `is_active`
- `role`: `"owner"` or `"admin"`
- `last_access`: ISO-8601, or `null` for a user who has never logged in

**Frontend.** This block extends `CleaningProviderUser` in `frontend/src/lib/dolcecleaning/types.ts`, adds the `TeamClient` and `providerTeamClient` in `frontend/src/lib/dolcecleaning/api.ts`, and adds the `AccountTeam` component in `frontend/src/components/dolceops/account-team/account-team.tsx`. `AccountTeam` uses `DataTable` from `frontend/src/components/core/data-table.tsx` and has these columns:
- Nome e cognome;
- Username;
- Email;
- Ruolo: `Titolare` or `Amministratore`;
- Stato: a chip, `Attiva` or `Disattivata`. This replaces "Attivo"/"Disattivo".
- Ultimo accesso: `dayjs(last_access).tz(ITALY_TZ).format("DD/MM/YYYY HH:mm")`, or `Mai`;
- Azioni.

**Page wiring.** The Utenti tab of `frontend/src/app/dolceops/dolcecleaning/impresa/[impresaId]/page.tsx` mounts `<AccountTeam client={providerTeamClient(impresaId, csrfToken)} …/>`.
- `onUsersLoaded` keeps `users` filled for the tab label and the destination dialog options.
- "Crea recapito dal numero telefono" moves to `renderExtraRowActions` and is shown when `tel` is set.
- `usersColumns` and the `listProviderUsers` export are removed.

**Backend tests:**
- the payload carries `role`, `last_access` and `restricted_actions_allowed`, and a deactivated user is listed with `is_active: false`;
- these callers get 403 with `{"success": false}` and no items:
  - an owner;
  - an OM user;
  - the Owner and an Admin of the same company;
  - a user of another company;
- an anonymous caller gets 419;
- the view declares its gate explicitly. With `view = resolve(reverse("dolcecleaning_provider_users", ...)).func`, the test asserts all of these:
  - `view.cls.permission_classes is not APIView.permission_classes`. DRF's `api_view` falls back to that default list object when the function has no `permission_classes`, so this is red before the decorator is added.
  - `view.cls.permission_classes[0] is IsAuthenticated`
  - `IsOperatore in view.cls.permission_classes`
- last access follows a real login. The cleaning user logs in through `POST api/react-login/` (`ReactLoginView`, not `force_login`). An operator's `GET …/users` then shows that user's `last_access` as no longer `null`, at a time between timestamps taken just before and just after the login request.

**Jest tests** in `frontend/src/components/dolceops/account-team/__tests__/`, using a fake `TeamClient`:
- `Mai` for `last_access: null`;
- `"2026-10-20T07:15:00Z"` renders as `20/10/2026 09:15`;
- exactly one `Titolare` among three rows;
- `Attiva` and `Disattivata`.

Interfaces:
- Produces: `GET api/dolcecleaning/providers/<int:provider_id>/users` → `{"ok": true, "items": AccountTeamUser[], "restricted_actions_allowed": boolean}`
- Produces: `def _team_entry_to_dict(entry) -> dict[str, Any]` (keys `id, user_id, username, first_name, last_name, email, tel, is_active, role, last_access`)
- Produces: `export type TeamRole = "owner" | "admin";`
- Produces: `export interface CleaningProviderUser { id: number; user_id: number; username: string; first_name: string; last_name: string; email: string; tel: string | null; is_active: boolean; role: TeamRole; last_access: string | null; }`
- Produces: `export type AccountTeamUser = CleaningProviderUser;`
- Produces: `export interface TeamList { items: AccountTeamUser[]; restricted_actions_allowed: boolean; }`
- Produces: `export interface TeamClient { list(): Promise<TeamList>; }`
- Produces: `export function providerTeamClient(providerId: number, csrfToken: string): TeamClient`
- Produces: `export function AccountTeam(props: { client: TeamClient; onUsersLoaded?: (users: AccountTeamUser[]) => void; renderExtraRowActions?: (u: AccountTeamUser) => React.ReactNode }): JSX.Element`
- Consumes: Block 1: `def _provider_account(provider_id: int) -> tuple[ImpresaPulizie, Account] | None`
- Consumes: account-user-api: `DolceSystem.account_user_service.list_users(account: Account, caller: User) -> AccountUserList`. The result has `.entries: list[AccountUserEntry]` and `.restricted_actions_allowed: bool`. Each `AccountUserEntry` has `utente: Utente`, `role: Literal["owner", "admin"]`, `is_active: bool` and `last_access: datetime | None`. These names are the contract account-user-api must provide.

Satisfies: dolcecleaning-users-tab (Utenti tab shows role, status and last access: Company with an Owner and an Admin, Deactivated user is listed, Last access updates after login), dolcecleaning-user-endpoint-access (Cleaning-company user management is operator-only: Non-operator tries to list a company's users, Anonymous caller), dolcecleaning-login-continuity (Owner and Admin see the same feed and nothing else: Cleaning Owner cannot manage users), account-team-table (Role labels; Status labels; Last access)

## 3. The team table on the DolceOps account sheet

**Client factory.** This block adds `accountTeamClient(accountId, csrfToken)` to `frontend/src/lib/dolcecleaning/api.ts`.
- Its `list()` calls account-user-api's list-users route for the account and maps the response to `TeamList`, including `restricted_actions_allowed` from the caller's restricted-actions flag.
- It normalises account-user-api's error field into `Error.message`, so `AccountTeam` reads errors the same way for both clients.
- That factory is the only place in the frontend that builds account-user-api URLs.

**Mount.** The account sheet page from `dolceops-account-list` gets one line: `<AccountTeam client={accountTeamClient(accountId, csrfToken)} />`. That is this block's only edit to that file.

**Jest tests** (mocked `fetch`):
- an account with three users, one deactivated, renders three rows, each with name and surname, username, email, role, status and last access;
- an empty `items` renders no rows;
- a non-2xx response shows its error message in the sheet.

To run the slice by hand, open an account sheet as an operator.

Interfaces:
- Produces: `export function accountTeamClient(accountId: number, csrfToken: string): TeamClient`
- Consumes: Block 2: `export interface TeamClient { list(): Promise<TeamList>; }`
- Consumes: Block 2: `export function AccountTeam(props: { client: TeamClient; onUsersLoaded?: (users: AccountTeamUser[]) => void; renderExtraRowActions?: (u: AccountTeamUser) => React.ReactNode }): JSX.Element`
- Consumes: account-user-api: `GET /api/accounts/<int:account_id>/users/` → 200 `{"items": [{"id": int (Utente.id), "user_id": int, "username": str, "first_name": str, "last_name": str, "email": str, "tel": str|null, "role": "owner"|"admin", "status": "active"|"deactivated", "last_access": ISO-8601|null}], "caller_in_restricted_list": bool}`. Any non-2xx response has the body `{"success": false, "error": str}`. The mapping to `TeamList` is:
  - `status === "active"` → `is_active`;
  - `role` → `TeamRole`, unchanged;
  - `caller_in_restricted_list` → `restricted_actions_allowed`;
  - `error` → `Error.message`.

  These names are the contract account-user-api must provide. Later blocks map an entry in the same way.

Satisfies: account-team-table (Team table on the account sheet: Every user of the account is listed)

## 4. Add a user from the Utenti tab and the account sheet

**Backend.** `provider_user_create_api` declares `@permission_classes([IsAuthenticated, IsOperatore])`. It calls account-user-api's add operation with:
- the account;
- `first_name` and `last_name`;
- `email` and `tel`, each optional;
- `password`, as `None` when empty;
- `send_email`;
- `actor=request.user`.

Any `role` or `username` in the body is ignored. The view returns 201 with `{"ok": true, "item": <entry>, "generated_password": "<clear text>"}`.

This removes from `views.py`:
- the inline `sanitize`/username loop, whose check today is case-sensitive (`User.objects.filter(username=...)`);
- the `secrets` password generator;
- the email check;
- the `User`/`Utente` creation.

`_service_error_response` maps service errors to `_json_error`:
- `DuplicateEmailError` (duplicate email) → 409 `Esiste già un utente con questa email`;
- `AccountUserValidationError` (validation, including a welcome email requested with no email) → 400 with the exception's `.message`.

**Frontend.**
- `TeamClient.add` is implemented in both factories; `providerTeamClient` maps `generated_password` to `password`.
- New `AddUserDialog` and `PasswordOnceDialog` in `team-dialogs.tsx`.
- The header action "Aggiungi utente" is always shown.

The add form:
- has Nome\*, Cognome\*, Email, Telefono and an optional Password, and no username field;
- shows a disabled Ruolo field: `Titolare` when the loaded list (deactivated users included) is empty, otherwise `Amministratore`. The role is never sent.
- has the checkbox `Invia email con credenziali`. It opens unchecked, is disabled while the trimmed email is empty, unchecks when the email is cleared, and sets `send_email`.
- keeps submit disabled while name or surname is empty, or while a non-empty email fails `/^[^\s@]+@[^\s@]+\.[^\s@]+$/`;
- shows a server error in an inline `Alert` and keeps the entered values.

On success, `PasswordOnceDialog` shows the username and password, with a copy button that calls `navigator.clipboard.writeText`. Its state is cleared on close.

`page.tsx` loses `generateStrongPassword` (`Math.random`), the auto-password toggle and `newUserSendEmail`. `createProviderUser` is no longer exported.

**Backend tests:**
- the first user of an empty company is Owner, and a second user is Admin;
- `"role": "owner"` is ignored when the company already has an Owner;
- a company whose users are all deactivated gets an Admin;
- a duplicate email with different letter case, belonging to a deactivated user, is refused with 409, and the user list is unchanged;
- if a duplicate refuses the first user, the company stays empty, and the next user to succeed becomes Owner;
- `send_email: true` puts exactly one message in `mail.outbox`, naming the company's account name; `false` sends none;
- these callers get 403 and nothing is created: an owner, an OM user, a user of the same company, and a user of another company. An anonymous caller gets 419. An operator gets 201.
- the create view declares its gate explicitly: `view.cls.permission_classes is not APIView.permission_classes`, `view.cls.permission_classes[0] is IsAuthenticated` and `IsOperatore in view.cls.permission_classes`;
- the first user joins the existing account. The company is created through `POST api/dolcecleaning/providers`. The test records its account and the account count, then adds the first user through `…/users/create`. It asserts that:
  - the user's account is the recorded one;
  - the role is `owner`;
  - the account count is unchanged.
- the Owner and an Admin see the same feed. The company has an Owner and a property with a `CleaningInfo` linked to it. An Admin is added through `…/users/create`. Both users log in through `POST api/react-login/` with their returned credentials. Their `GET api/calendario-pulizie/` responses have the same items, and those items are not empty.
- legacy rows are unchanged after an add (`assert_legacy_impresa_rows_unchanged`).

**Jest tests:**
- the role label in both states;
- no username field;
- checkbox enabling, disabling and unchecking;
- `send_email` sent only when the box is checked;
- the submit button blocked for an empty surname;
- the password visible once and not after the dialog is closed;
- a refusal keeps the values and adds no row;
- an account sheet whose `accountTeamClient` list returns no items shows no rows and still offers "Aggiungi utente".

Interfaces:
- Produces: `POST api/dolcecleaning/providers/<int:provider_id>/users/create` body `{first_name: str, last_name: str, email?: str|null, tel?: str|null, password?: str|null, send_email: bool}` → 201 `{"ok": true, "item": <entry>, "generated_password": str}`
- Produces: `def _service_error_response(exc: Exception) -> JsonResponse`
- Produces: `export interface AddTeamUserPayload { first_name: string; last_name: string; email?: string | null; tel?: string | null; password?: string | null; send_email: boolean; }`
- Produces: `TeamClient.add(p: AddTeamUserPayload): Promise<{ item: AccountTeamUser; password: string }>`
- Produces: `export function PasswordOnceDialog(props: { open: boolean; username?: string; password: string; onClose: () => void }): JSX.Element`
- Consumes: Block 1: `def _provider_account(provider_id: int) -> tuple[ImpresaPulizie, Account] | None`
- Consumes: Block 2: `def _team_entry_to_dict(entry) -> dict[str, Any]`
- Consumes: Block 2: `export function providerTeamClient(providerId: number, csrfToken: string): TeamClient`
- Consumes: Block 3: `export function accountTeamClient(accountId: number, csrfToken: string): TeamClient`
- Consumes: account-user-api: `DolceSystem.account_user_service.add_user(account: Account, *, first_name: str, last_name: str, email: str | None, tel: str | None, password: str | None, send_welcome_email: bool, actor: User) -> AddedUser`. `AddedUser` has `.entry: AccountUserEntry` and `.password: str`.
- Consumes: account-user-api: these exceptions in `DolceSystem.account_user_service`:
  - `AccountUserError(Exception)`, which has `.message: str`;
  - its subclasses `DuplicateEmailError` and `AccountUserValidationError`.
- Consumes: account-user-api: `POST /api/accounts/<int:account_id>/users/` body `{first_name, last_name, email, tel, password, send_welcome_email}` → 201 `{"item": <entry as in Block 3>, "password": str}`. `accountTeamClient.add` maps `send_email` → `send_welcome_email`.

Satisfies: dolcecleaning-users-tab ("Nuovo Utente" role is automatic and read-only: all four scenarios; "Nuovo Utente" credentials and optional fields: all four scenarios; Welcome email checkbox on the add-user forms: all four scenarios), dolcecleaning-user-validation (Username and email uniqueness on creation: Duplicate email of a deactivated user, Refused first user leaves the company without users), dolcecleaning-user-endpoint-access (Logged-in owner tries to create a cleaning user, Cleaning user tries to manage their own company's users, Cleaning user tries to manage another company's users, Operator manages a company's users), account-team-add-user (all requirements), account-team-table (Team table on the account sheet: Account without users; Table reflects each action without a manual reload; Failed action leaves the table unchanged), dolcecleaning-company-account (A cleaning company is an account even with no users: First user joins the existing account), dolcecleaning-login-continuity (Owner and Admin see the same feed and nothing else: Owner and Admin compare feeds)

## 5. Edit a user's personal data, refusing a duplicate email

**Backend.** `provider_user_detail_api` declares `@permission_classes([IsAuthenticated, IsOperatore])`. `_account_utente` resolves `utente_id` within the company's account; a user of another company gets 404 `Utente non trovato per questa impresa`. PUT calls account-user-api's edit operation with `first_name`, `last_name`, `email` and `tel` only. It ignores `is_active`, `username` and `role`, so status changes only through the routes in later blocks. A duplicate email returns 409 `Esiste già un utente con questa email`.

**Frontend.**
- `TeamClient.edit` is implemented in both factories.
- `EditUserDialog` has Nome, Cognome, Email and Telefono. It has no role control and no "Attivo" switch.
- A refusal shows an inline `Alert`, keeps the values and leaves the row's previous email.
- A missing CSRF token shows `toast.error("CSRF token mancante")` instead of today's silent return.
- `updateProviderUser` is no longer exported.

**Backend tests:**
- these new emails get 409 and the email stays unchanged:
  - an active OM user's email;
  - `Mario.Rossi@Example.com` against `mario.rossi@example.com`;
  - a deactivated user's email;
- editing only the phone with the same email succeeds, and so does `mario@example.com` → `Mario@example.com`;
- `is_active: false` in a PUT leaves the user active;
- the 403/419 matrix for PUT;
- the detail view declares its gate explicitly: `view.cls.permission_classes is not APIView.permission_classes`, `[0] is IsAuthenticated`, and `IsOperatore` included;
- legacy rows are unchanged.

**Jest tests:**
- the edited surname shows after save;
- the form has no role control;
- a duplicate email keeps the dialog open with the message, and the table shows the old email.

Interfaces:
- Produces: `def _account_utente(account: Account, utente_id: int) -> Utente | None`
- Produces: `PUT api/dolcecleaning/providers/<int:provider_id>/users/<int:utente_id>` body `{first_name: str, last_name: str, email: str|null, tel: str|null}` → `{"ok": true, "item": <entry>}`
- Produces: `TeamClient.edit(id: number, p: { first_name: string; last_name: string; email: string | null; tel: string | null }): Promise<AccountTeamUser>`
- Consumes: Block 1: `def _provider_account(provider_id: int) -> tuple[ImpresaPulizie, Account] | None`
- Consumes: Block 2: `def _team_entry_to_dict(entry) -> dict[str, Any]`
- Consumes: Block 4: `def _service_error_response(exc: Exception) -> JsonResponse`
- Consumes: account-user-api: `DolceSystem.account_user_service.edit_user(account: Account, utente: Utente, *, first_name: str, last_name: str, email: str | None, tel: str | None) -> AccountUserEntry`. It raises `DuplicateEmailError` or `AccountUserValidationError`.
- Consumes: account-user-api: `PATCH /api/accounts/<int:account_id>/users/<int:user_id>/` (`user_id` = `Utente.id`) body `{first_name, last_name, email, tel}` → 200 `{"item": <entry as in Block 3>}`

Satisfies: dolcecleaning-user-validation (Email uniqueness on edit: all five scenarios), dolcecleaning-users-tab (Editing a user's details: Successful edit), account-team-edit-user (Edit personal data: Details updated, Role not editable), dolcecleaning-user-endpoint-access (Logged-in OM user tries to deactivate a cleaning user is extended to edit; Cleaning user tries to manage another company's users)

## 6. Deactivate and reactivate a non-Owner user, after confirmation

**Backend.**
- `DELETE …/users/<int:utente_id>` keeps its meaning of "deactivate". It calls account-user-api's deactivate operation, which ends the user's sessions, and replaces the `is_active` write.
- When the target is the Owner, it returns 400 and the Owner stays active. The service raises `OwnerRuleError`, and this block adds that exception to `_service_error_response` as 400 with its `.message`.
- New route `POST api/dolcecleaning/providers/<int:provider_id>/users/<int:utente_id>/reactivate`, named `dolcecleaning_provider_user_reactivate`, view `provider_user_reactivate_api`, with `@permission_classes([IsAuthenticated, IsOperatore])`.

**Frontend.**
- `TeamClient.deactivate` and `TeamClient.reactivate` are implemented in both factories.
- New `ConfirmActionDialog`, built from the pattern of `page.tsx`'s `confirmDialog`.
- Row actions: `Disattiva` when `is_active && role === "admin"`, so the Owner never gets the ordinary action; `Riattiva` when `!is_active`.
- Cancelling sends no request. A refusal shows `toast.error(e.message)` and leaves the rows unchanged.
- `deleteProviderUser` and the old `handleDeactivateUser` are removed.

**Backend tests:**
- after deactivation, login fails;
- a request with the user's open session gets 419;
- another user's session keeps working;
- after reactivation, the user logs in with the original password;
- DELETE on the Owner gives 400 and the Owner stays active;
- an OM user calling DELETE gets 403 and the target stays active;
- the 403/419 matrix covers the new route, which is also added to `_richieste()` in `backend/DolceCleaning/tests/test_drf_pannello_auth.py`;
- the reactivate view declares its gate explicitly: `view.cls.permission_classes is not APIView.permission_classes`, `[0] is IsAuthenticated`, and `IsOperatore` included;
- an existing cleaning user keeps logging in after a colleague is deactivated. The setup is:
  - a cleaning user created the pre-release way, directly as `User` + `Utente` with `impresa_pulizie_pms`;
  - a company property with a `CleaningInfo` linked to it.

  Another user of the same company is deactivated through DELETE. The untouched user then logs in through `POST api/react-login/` with their unchanged username and password, and `GET api/calendario-pulizie/` returns the company's cleanings;
- legacy rows are unchanged.

**Jest tests:**
- confirming shows `Disattivata` with no reload;
- cancelling makes no client call;
- the Owner row has no `Disattiva`;
- reactivating shows `Attiva`;
- a rejected call leaves the rows unchanged.

Interfaces:
- Produces: `POST api/dolcecleaning/providers/<int:provider_id>/users/<int:utente_id>/reactivate` (`dolcecleaning_provider_user_reactivate`) → `{"ok": true}`
- Produces: `TeamClient.deactivate(id: number): Promise<void>`; `TeamClient.reactivate(id: number): Promise<void>`
- Produces: `export function ConfirmActionDialog(props: { open: boolean; title: string; message: string; confirmLabel: string; onConfirm: () => void | Promise<void>; onCancel: () => void }): JSX.Element`
- Consumes: Block 5: `def _account_utente(account: Account, utente_id: int) -> Utente | None`
- Consumes: Block 4: `def _service_error_response(exc: Exception) -> JsonResponse`
- Consumes: account-user-api: these functions and exception in `DolceSystem.account_user_service`:
  - `deactivate_user(account: Account, utente: Utente) -> None`, which ends the user's sessions and raises `OwnerRuleError` on the Owner;
  - `reactivate_user(account: Account, utente: Utente) -> None`;
  - `OwnerRuleError(AccountUserError)`.
- Consumes: account-user-api: `POST /api/accounts/<int:account_id>/users/<int:user_id>/deactivate/` and `POST /api/accounts/<int:account_id>/users/<int:user_id>/reactivate/` → 200 `{"success": true}`

Satisfies: dolcecleaning-users-tab (Deactivation and reactivation from the Utenti tab: Deactivate an Admin, Deactivation cancelled, Reactivate a user), dolcecleaning-user-endpoint-access (Logged-in OM user tries to deactivate a cleaning user), account-team-activation (all requirements), dolcecleaning-login-continuity (Existing cleaning users keep logging in as before: Existing user logs in after release)

## 7. Change the Owner, limited to the configured operators

**Backend.** New route `POST api/dolcecleaning/providers/<int:provider_id>/owner`, named `dolcecleaning_provider_owner_change`, view `provider_owner_change_api`. Its body is `{"utente_id": int}`, and it has `@permission_classes([IsAuthenticated, IsOperatore, IsRestrictedActionsOperatore])`. The last class is imported from `DolceEngine/endpoint_guard/permissions.py`, not defined here. It calls account-user-api's change-Owner operation:
- a target who is already Owner, or is deactivated, gets 400;
- a user outside the company gets 404 `Utente non trovato per questa impresa`.

**Frontend.**
- `TeamClient.changeOwner` is implemented in both factories.
- The header action "Cambia titolare" is rendered only when `restricted_actions_allowed` is true and at least one active non-Owner user exists. When the flag is false it is absent from the DOM, not disabled.
- `ChangeOwnerDialog` lists only active users other than the Owner, then confirms.

**Backend tests:**
- an unlisted operator gets 403 and roles are unchanged. This uses `override_settings(ACCOUNT_RESTRICTED_ACTIONS_OPERATORS=[...])`, with a list that leaves the caller out;
- an empty list refuses everyone;
- a listed operator turns Admin B into `owner` and A into `admin`, leaving exactly one Owner;
- the view's `permission_classes` contains `IsRestrictedActionsOperatore`;
- the route is added to `test_drf_pannello_auth.py`.

**Jest tests:**
- the action is absent when the flag is false;
- the candidate list excludes the Owner and deactivated users;
- the action is not offered when there is no candidate;
- the refreshed table shows exactly one `Titolare`;
- a 403 shows its message and leaves the table unchanged.

Interfaces:
- Produces: `POST api/dolcecleaning/providers/<int:provider_id>/owner` (`dolcecleaning_provider_owner_change`) body `{"utente_id": int}` → `{"ok": true}`
- Produces: `TeamClient.changeOwner(id: number): Promise<void>`
- Consumes: Block 6: `export function ConfirmActionDialog(props: { open: boolean; title: string; message: string; confirmLabel: string; onConfirm: () => void | Promise<void>; onCancel: () => void }): JSX.Element`
- Consumes: Block 5: `def _account_utente(account: Account, utente_id: int) -> Utente | None`
- Consumes: Block 2: `export interface TeamList { items: AccountTeamUser[]; restricted_actions_allowed: boolean; }`
- Consumes: account-user-api: `DolceSystem.account_user_service.change_owner(account: Account, utente: Utente) -> None`. It raises `OwnerRuleError` when the target is already Owner or is deactivated.
- Consumes: account-user-api: the permission class `IsRestrictedActionsOperatore` in `backend/DolceEngine/endpoint_guard/permissions.py`.
- Consumes: account-user-api: the setting `ACCOUNT_RESTRICTED_ACTIONS_OPERATORS: list[str]` (operator usernames) in `backend/DolceEngine/settings.py`.
- Consumes: account-user-api: `POST /api/accounts/<int:account_id>/owner/` body `{"user_id": int}` (`Utente.id`) → 200 `{"success": true}`.

Satisfies: account-team-restricted-actions (Restricted actions shown only to listed operators: Server refusal is surfaced; Change the Owner: Owner changed, Candidates for Owner, No candidate), dolcecleaning-users-tab (Change Owner, delete user and reset password from the Utenti tab: Change the company's Owner)

## 8. Deactivate the Owner and delete a user, limited to the configured operators

**Backend.** Two new routes, each with `@permission_classes([IsAuthenticated, IsOperatore, IsRestrictedActionsOperatore])`:
- `POST …/users/<int:utente_id>/deactivate-owner`, named `dolcecleaning_provider_user_deactivate_owner`. It calls deactivate-Owner, and a non-Owner target gets 400.
- `POST …/users/<int:utente_id>/delete`, named `dolcecleaning_provider_user_delete`. It calls delete, and an Owner target gets 400.

**Frontend.**
- `TeamClient.deactivateOwner` and `TeamClient.remove` are implemented in both factories.
- `Disattiva titolare` is shown when `R && role === "owner" && is_active`.
- `Elimina` is shown when `R && role === "admin"`, so it never appears on the Owner's row. Its confirmation message contains `L'operazione non può essere annullata.`

**Backend tests:**
- an unlisted operator gets 403 on both routes, and the Owner stays active and the user stays in place;
- a listed operator deactivates the Owner: the user stays `owner`, sessions end and login fails;
- deleting an Admin of a company with properties and three users:
  - leaves the other two users and the company's `CleaningInfo` rows;
  - keeps any `CleaningMessageDestination` that pointed at the user, with `utente` set to null;
- deleting the Owner gives 400;
- both routes are added to `test_drf_pannello_auth.py`;
- legacy rows are unchanged.

**Jest tests:**
- the Owner row shows `Titolare` and `Disattivata` after deactivate-owner;
- after a delete the row is gone, and cancelling makes no call;
- no `Elimina` on the Owner;
- both actions are absent when the flag is false.

Interfaces:
- Produces: `POST api/dolcecleaning/providers/<int:provider_id>/users/<int:utente_id>/deactivate-owner` (`dolcecleaning_provider_user_deactivate_owner`) → `{"ok": true}`
- Produces: `POST api/dolcecleaning/providers/<int:provider_id>/users/<int:utente_id>/delete` (`dolcecleaning_provider_user_delete`) → `{"ok": true}`
- Produces: `TeamClient.deactivateOwner(id: number): Promise<void>`; `TeamClient.remove(id: number): Promise<void>`
- Consumes: Block 6: `export function ConfirmActionDialog(props: { open: boolean; title: string; message: string; confirmLabel: string; onConfirm: () => void | Promise<void>; onCancel: () => void }): JSX.Element`
- Consumes: Block 5: `def _account_utente(account: Account, utente_id: int) -> Utente | None`
- Consumes: account-user-api: `DolceSystem.account_user_service.deactivate_owner(account: Account, utente: Utente) -> None` and `delete_user(account: Account, utente: Utente) -> None`. Both raise `OwnerRuleError`: the first for a non-Owner target, the second for the Owner.
- Consumes: account-user-api: `IsRestrictedActionsOperatore` and `ACCOUNT_RESTRICTED_ACTIONS_OPERATORS`, as in Block 7.
- Consumes: account-user-api: `POST /api/accounts/<int:account_id>/users/<int:user_id>/deactivate-owner/` and `POST /api/accounts/<int:account_id>/users/<int:user_id>/delete/` → 200 `{"success": true}`.

Satisfies: account-team-restricted-actions (Deactivate the Owner: Owner deactivated; Delete a user: User deleted, Delete cancelled, No delete on the Owner), dolcecleaning-users-tab (Deactivation and reactivation from the Utenti tab: Deactivating the Owner by a non-authorised operator, Deactivating the Owner by an authorised operator; Change Owner, delete user and reset password from the Utenti tab: Delete an Admin, No delete on the Owner)

## 9. Reset a password, shown once, limited to the configured operators

**Backend.** New route `POST api/dolcecleaning/providers/<int:provider_id>/users/<int:utente_id>/reset-password`, named `dolcecleaning_provider_user_reset_password`. It has `@permission_classes([IsAuthenticated, IsOperatore, IsRestrictedActionsOperatore])`, takes the body `{"password": str | null}`, calls account-user-api's reset operation and returns `{"ok": true, "password": "<new clear text>"}`.

If a chosen password fails `validate_password_strength` (`backend/DolceSystem/endpointz/update_user.py`), the route returns 400 with that message and the old password still works.

**Frontend.**
- `TeamClient.resetPassword` is implemented in both factories.
- Row action `Reimposta password` is shown when `R`.
- `ResetPasswordDialog` has an optional password field and confirms before sending.
- The result appears in `PasswordOnceDialog` with its copy control. After closing, it never appears again in rows, cache, `localStorage` or the URL.

**Backend tests:**
- a generated reset lets the user log in with the new password only;
- a chosen valid password is returned as typed;
- an invalid one gives 400 and the old password keeps working;
- an unlisted operator gets 403;
- the route is added to `test_drf_pannello_auth.py`;
- an operator outside the list is refused every restricted route, now that all of them exist. The caller is left out with `override_settings(ACCOUNT_RESTRICTED_ACTIONS_OPERATORS=[...])`. Direct calls to `owner`, `users/<id>/deactivate-owner`, `users/<id>/delete` and `users/<id>/reset-password` each get 403, and every user's role, status, existence and password are unchanged. The same operator still succeeds on create, PUT, DELETE (deactivate) and reactivate.

**Jest tests:**
- the password is shown once with a copy button;
- reopening the user after closing does not show it;
- a typed password is the one displayed;
- the action is absent when the flag is false;
- the restricted actions follow the flag:
  - with `restricted_actions_allowed: true`, an Owner and an active Admin, the table offers `Cambia titolare`, `Disattiva titolare`, `Elimina` and `Reimposta password`;
  - with the flag false, none of the four is in the DOM. `Aggiungi utente`, `Modifica`, `Disattiva` (on the Admin) and `Riattiva` (on a deactivated row) are still shown.

Interfaces:
- Produces: `POST api/dolcecleaning/providers/<int:provider_id>/users/<int:utente_id>/reset-password` (`dolcecleaning_provider_user_reset_password`) body `{"password": str | null}` → `{"ok": true, "password": str}`
- Produces: `TeamClient.resetPassword(id: number, password: string | null): Promise<{ password: string }>`
- Consumes: Block 4: `export function PasswordOnceDialog(props: { open: boolean; username?: string; password: string; onClose: () => void }): JSX.Element`
- Consumes: Block 6: `export function ConfirmActionDialog(props: { open: boolean; title: string; message: string; confirmLabel: string; onConfirm: () => void | Promise<void>; onCancel: () => void }): JSX.Element`
- Consumes: Block 5: `def _account_utente(account: Account, utente_id: int) -> Utente | None`
- Consumes: account-user-api: `DolceSystem.account_user_service.reset_password(account: Account, utente: Utente, password: str | None) -> str`. It returns the new clear-text password. It raises `AccountUserValidationError` with the `validate_password_strength` message.
- Consumes: account-user-api: `IsRestrictedActionsOperatore` and `ACCOUNT_RESTRICTED_ACTIONS_OPERATORS`, as in Block 7.
- Consumes: account-user-api: `POST /api/accounts/<int:account_id>/users/<int:user_id>/reset-password/` body `{"password": str|null}` → 200 `{"password": str}`.

Satisfies: account-team-restricted-actions (Restricted actions shown only to listed operators: Operator outside the list, Operator in the list; Reset a password: New password shown once, Operator-chosen password), dolcecleaning-users-tab (Change Owner, delete user and reset password from the Utenti tab: Operator outside the list, Reset a password)