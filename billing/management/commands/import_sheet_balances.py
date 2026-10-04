"""Import member opening balances from the treasurer's spreadsheet.

Input is a CSV with the columns ``name, email, ssa_number, status,
sheet_balance``. ``sheet_balance`` uses the spreadsheet's sign convention:
positive means the member has a credit with the club, negative means the
member owes the club. Billing uses the opposite convention, so amounts are
flipped when posted.

Active members get one opening-balance entry. Any other status (Inactive,
Terminated, ...) gets the opening balance followed by a same-day write-off,
so the account sits at zero but the prior balance stays visible for a
true-up if the member rejoins. With ``--create-missing-inactive``, former
members with no account get a login-disabled Inactive member record first.

The command is a dry run unless ``--commit`` is given. A committed run is
all-or-nothing and can be repeated safely: entries that already exist with
the same values are left alone.
"""

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from billing.exceptions import BillingDisabledError
from billing.models import LedgerEntry
from billing.permissions import require_manual_transaction_access
from billing.services import post_entry, post_opening_balance
from members.models import Member

REQUIRED_COLUMNS = {"name", "email", "ssa_number", "status", "sheet_balance"}
ACTIVE_STATUS = "active"
INACTIVE_MEMBERSHIP_STATUS = "Inactive"
NO_MATCH = "no member matches SSA number, email, or name"


@dataclass
class Row:
    line: int
    name: str
    email: str
    ssa_number: str
    status: str
    sheet_balance: Decimal
    member: Member | None = None
    problem: str = ""
    create: bool = False

    @property
    def is_active(self):
        return self.status.strip().lower() == ACTIVE_STATUS

    @property
    def effect(self):
        # Sheet credit (positive) is a billing credit; sheet debt is a debit.
        if self.sheet_balance > 0:
            return LedgerEntry.Effect.CREDIT
        return LedgerEntry.Effect.DEBIT

    @property
    def amount(self):
        return abs(self.sheet_balance)

    @property
    def billing_balance(self):
        return -self.sheet_balance


def parse_money(value):
    text = (value or "").strip().replace("$", "").replace(",", "")
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").strip()
    if text.startswith("-"):
        negative, text = True, text[1:]
    try:
        amount = Decimal(text or "0").quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError(f"not a money amount: {value!r}") from exc
    return -amount if negative else amount


def read_rows(path):
    try:
        handle = open(path, newline="", encoding="utf-8-sig")
    except OSError as exc:
        raise CommandError(f"Cannot open {path}: {exc}") from exc
    with handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise CommandError(f"Missing column(s): {', '.join(sorted(missing))}")
        rows = []
        for line, raw in enumerate(reader, start=2):
            try:
                balance = parse_money(raw["sheet_balance"])
            except ValueError as exc:
                raise CommandError(f"Line {line}: {exc}") from exc
            rows.append(
                Row(
                    line=line,
                    name=(raw["name"] or "").strip(),
                    email=(raw["email"] or "").strip(),
                    ssa_number=(raw["ssa_number"] or "").strip(),
                    status=(raw["status"] or "").strip(),
                    sheet_balance=balance,
                )
            )
    return rows


def match_member(row):
    """Match by SSA number, then email, then exact first/last name.

    Every identifier that finds someone must find the same member; any
    disagreement is reported rather than guessed.
    """
    found = {}
    if row.ssa_number:
        found["SSA number"] = list(
            Member.objects.filter(SSA_member_number=row.ssa_number)
        )
    if row.email:
        found["email"] = list(Member.objects.filter(email__iexact=row.email))
    first, _, last = row.name.partition(" ")
    if first and last:
        found["name"] = list(
            Member.objects.filter(
                Q(first_name__iexact=first) | Q(nickname__iexact=first),
                last_name__iexact=last,
            )
        )

    for label, members in found.items():
        if len(members) > 1:
            return None, f"{label} matches {len(members)} members"
    matches = {label: members[0] for label, members in found.items() if members}
    if not matches:
        return None, NO_MATCH
    distinct = {member.pk for member in matches.values()}
    if len(distinct) > 1:
        detail = ", ".join(
            f"{label} → {member.username}" for label, member in matches.items()
        )
        return None, f"identifiers disagree ({detail})"
    return next(iter(matches.values())), ""


def writeoff_source_key(member):
    return f"sheet-import:writeoff:{member.pk}"


def create_inactive_member(row):
    """Create a login-disabled Inactive member record for a former member."""
    first, _, last = row.name.partition(" ")
    base = f"{first}.{last}".lower().replace(" ", "")
    username, suffix = base, 2
    while Member.objects.filter(username=username).exists():
        username, suffix = f"{base}{suffix}", suffix + 1
    member = Member(
        username=username,
        first_name=first,
        last_name=last,
        email=row.email,
        SSA_member_number=row.ssa_number or None,
        membership_status=INACTIVE_MEMBERSHIP_STATUS,
        is_active=False,
    )
    member.set_unusable_password()
    member.save()
    return member


class Command(BaseCommand):
    help = (
        "Import opening balances from the treasurer's spreadsheet export. "
        "Dry run unless --commit is given."
    )

    def add_arguments(self, parser):
        parser.add_argument("csv_path", help="CSV of balances to import")
        parser.add_argument(
            "--date",
            required=True,
            type=date.fromisoformat,
            help="Effective date for every entry (YYYY-MM-DD)",
        )
        parser.add_argument(
            "--actor",
            required=True,
            help="Username of the treasurer or superuser posting the entries",
        )
        parser.add_argument(
            "--expect-active-total",
            type=parse_money,
            help=(
                "Net sheet balance of active members (sheet sign); the import "
                "stops if the file does not add up to it"
            ),
        )
        parser.add_argument(
            "--create-missing-inactive",
            action="store_true",
            help=(
                "Create login-disabled Inactive member records for non-active "
                "rows that match no existing member"
            ),
        )
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Write the entries (default is a dry run)",
        )

    def handle(self, *args, **options):
        try:
            actor = Member.objects.get(username=options["actor"])
        except Member.DoesNotExist as exc:
            raise CommandError(f"No member with username {options['actor']!r}") from exc
        try:
            require_manual_transaction_access(actor)
        except ValidationError as exc:
            raise CommandError(exc.messages[0]) from exc

        effective_date = options["date"]
        rows = read_rows(options["csv_path"])
        for row in rows:
            row.member, row.problem = match_member(row)
            if (
                row.problem == NO_MATCH
                and not row.is_active
                and options["create_missing_inactive"]
            ):
                row.create, row.problem = True, ""

        self.check_duplicates(rows)
        self.report(rows)

        active_total = sum(
            (r.sheet_balance for r in rows if r.is_active), Decimal("0.00")
        )
        expected = options["expect_active_total"]
        if expected is not None and active_total != expected:
            raise CommandError(
                f"Active sheet total {active_total} does not match expected {expected}."
            )

        problems = [r for r in rows if r.problem]
        if problems:
            raise CommandError(
                f"{len(problems)} row(s) could not be matched; nothing was written."
            )
        if not options["commit"]:
            self.stdout.write(self.style.WARNING("Dry run: nothing was written."))
            return

        try:
            with transaction.atomic():
                posted = sum(self.post_row(row, actor, effective_date) for row in rows)
        except BillingDisabledError as exc:
            raise CommandError(
                "Billing is disabled; enable it in Site Configuration first."
            ) from exc
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc
        self.stdout.write(self.style.SUCCESS(f"Posted {posted} new entr(ies)."))

    def check_duplicates(self, rows):
        seen = {}
        for row in rows:
            if row.member is None:
                continue
            first = seen.setdefault(row.member.pk, row)
            if first is not row:
                row.problem = (
                    f"same member as line {first.line} ({row.member.username})"
                )

    def report(self, rows):
        for row in rows:
            if row.problem:
                self.stdout.write(
                    self.style.ERROR(f"Line {row.line} {row.name}: {row.problem}")
                )
                continue
            action = "opening balance" if row.is_active else "opening + write-off"
            if row.sheet_balance == 0:
                action = "zero balance, skipped"
            target = "NEW Inactive member" if row.create else row.member.username
            self.stdout.write(
                f"Line {row.line} {row.name} → {target} "
                f"[{row.status}] billing balance {row.billing_balance:+.2f}: {action}"
            )

        active = [r for r in rows if r.is_active]
        inactive = [r for r in rows if not r.is_active]
        owed = sum((r.billing_balance for r in active if r.billing_balance > 0), 0)
        credit = sum((r.billing_balance for r in active if r.billing_balance < 0), 0)
        self.stdout.write(
            f"Active: {len(active)} member(s), owed to club {owed:.2f}, "
            f"credits held {-credit:.2f}, net sheet total "
            f"{sum((r.sheet_balance for r in active), Decimal('0.00')):.2f}"
        )
        self.stdout.write(
            f"Non-active: {len(inactive)} member(s) written off to zero, "
            f"net set aside {sum((r.billing_balance for r in inactive), 0):+.2f}"
        )

    def post_row(self, row, actor, effective_date):
        if row.sheet_balance == 0:
            return 0
        if row.create:
            row.member = create_inactive_member(row)
            self.stdout.write(f"Created Inactive member {row.member.username}")
        posted = 0
        existing = LedgerEntry.objects.filter(
            ledger__member=row.member, kind=LedgerEntry.Kind.OPENING_BALANCE
        ).first()
        if existing is None:
            post_opening_balance(
                member=row.member,
                actor=actor,
                amount=row.amount,
                effect=row.effect,
                effective_date=effective_date,
                description="Opening balance carried over from spreadsheet ledger",
                reason=f"Imported from treasurer's spreadsheet ({row.status})",
            )
            posted += 1
        elif (
            existing.amount != row.amount
            or existing.effect != row.effect
            or existing.effective_date != effective_date
        ):
            raise ValidationError(
                f"{row.name} already has a different opening balance "
                f"({existing.effect} {existing.amount} on {existing.effective_date})."
            )

        if not row.is_active:
            source_key = writeoff_source_key(row.member)
            already = LedgerEntry.objects.filter(source_key=source_key).exists()
            owes = row.effect == LedgerEntry.Effect.DEBIT
            post_entry(
                member=row.member,
                actor=actor,
                kind=(
                    LedgerEntry.Kind.CREDIT if owes else LedgerEntry.Kind.MANUAL_CHARGE
                ),
                effect=(
                    LedgerEntry.Effect.CREDIT if owes else LedgerEntry.Effect.DEBIT
                ),
                amount=row.amount,
                effective_date=effective_date,
                description=(
                    f"{row.status} member balance set aside; "
                    f"prior balance {row.billing_balance:+.2f}, true-up at rejoin"
                ),
                internal_note=(
                    f"Written off at spreadsheet cutover ({row.status}); "
                    "not counted against the General Fund"
                ),
                source_key=source_key,
            )
            posted += 0 if already else 1
        return posted
