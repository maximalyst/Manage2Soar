from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from billing.models import LedgerEntry
from billing.services import get_balance
from members.models import Member
from siteconfig.models import SiteConfiguration

HEADER = "name,email,ssa_number,status,sheet_balance\n"
CUTOVER = date(2026, 1, 15)


@pytest.fixture
def treasurer(db):
    return Member.objects.create_user(username="treasurer", treasurer=True)


@pytest.fixture
def active_owes(db):
    return Member.objects.create_user(
        username="owes",
        first_name="Dana",
        last_name="Owes",
        email="dana@example.com",
        SSA_member_number="1001",
        membership_status="Full Member",
    )


@pytest.fixture
def active_credit(db):
    return Member.objects.create_user(
        username="credit",
        first_name="Casey",
        last_name="Credit",
        email="casey@example.com",
        SSA_member_number="1002",
        membership_status="Full Member",
    )


@pytest.fixture
def inactive(db):
    return Member.objects.create_user(
        username="gone",
        first_name="Gale",
        last_name="Gone",
        email="gale@example.com",
        membership_status="Inactive",
    )


def write_csv(tmp_path, body):
    path = tmp_path / "balances.csv"
    path.write_text(HEADER + body)
    return str(path)


def run(path, *extra):
    out = StringIO()
    call_command(
        "import_sheet_balances",
        path,
        "--date",
        CUTOVER.isoformat(),
        "--actor",
        "treasurer",
        *extra,
        stdout=out,
    )
    return out.getvalue()


@pytest.fixture
def standard_csv(tmp_path, active_owes, active_credit, inactive):
    return write_csv(
        tmp_path,
        "Dana Owes,dana@example.com,1001,Active,($577.75)\n"
        'Casey Credit,CASEY@example.com,1002,Active,"$1,354.00"\n'
        "Gale Gone,gale@example.com,,Inactive,($263.00)\n",
    )


def test_dry_run_writes_nothing(treasurer, standard_csv):
    output = run(standard_csv)

    assert "Dry run" in output
    assert not LedgerEntry.objects.exists()


def test_commit_flips_sheet_sign_and_writes_off_inactive(
    treasurer, standard_csv, active_owes, active_credit, inactive
):
    run(standard_csv, "--commit")

    assert get_balance(active_owes.billing_ledger) == Decimal("577.75")
    assert get_balance(active_credit.billing_ledger) == Decimal("-1354.00")
    assert get_balance(inactive.billing_ledger) == Decimal("0.00")

    opening = inactive.billing_ledger.entries.get(kind=LedgerEntry.Kind.OPENING_BALANCE)
    assert opening.effect == LedgerEntry.Effect.DEBIT
    assert opening.amount == Decimal("263.00")
    assert opening.effective_date == CUTOVER
    writeoff = inactive.billing_ledger.entries.get(kind=LedgerEntry.Kind.CREDIT)
    assert writeoff.amount == Decimal("263.00")
    assert "true-up at rejoin" in writeoff.member_description


def test_inactive_credit_balance_is_written_off_with_a_charge(
    treasurer, tmp_path, inactive
):
    path = write_csv(tmp_path, "Gale Gone,gale@example.com,,Inactive,$224.05\n")

    run(path, "--commit")

    ledger = inactive.billing_ledger
    assert get_balance(ledger) == Decimal("0.00")
    assert ledger.entries.get(kind=LedgerEntry.Kind.MANUAL_CHARGE).amount == Decimal(
        "224.05"
    )


def test_commit_is_idempotent(treasurer, standard_csv):
    run(standard_csv, "--commit")
    count = LedgerEntry.objects.count()

    output = run(standard_csv, "--commit")

    assert LedgerEntry.objects.count() == count
    assert "Posted 0 new" in output


def test_changed_balance_on_rerun_is_rejected(treasurer, tmp_path, active_owes):
    run(
        write_csv(tmp_path, "Dana Owes,dana@example.com,1001,Active,($10.00)\n"),
        "--commit",
    )

    with pytest.raises(CommandError, match="different opening balance"):
        run(
            write_csv(tmp_path, "Dana Owes,dana@example.com,1001,Active,($20.00)\n"),
            "--commit",
        )


def test_unmatched_row_blocks_the_whole_import(treasurer, tmp_path, active_owes):
    path = write_csv(
        tmp_path,
        "Dana Owes,dana@example.com,1001,Active,($10.00)\n"
        "Nobody Here,nobody@example.com,,Active,$5.00\n",
    )

    with pytest.raises(CommandError, match="could not be matched"):
        run(path, "--commit")

    assert not LedgerEntry.objects.exists()


def test_conflicting_identifiers_are_not_guessed(
    treasurer, tmp_path, active_owes, active_credit
):
    # SSA number points at Dana, email points at Casey.
    path = write_csv(tmp_path, "Dana Owes,casey@example.com,1001,Active,$5.00\n")

    with pytest.raises(CommandError):
        output = StringIO()
        call_command(
            "import_sheet_balances",
            path,
            "--date",
            CUTOVER.isoformat(),
            "--actor",
            "treasurer",
            stdout=output,
        )
    assert "identifiers disagree" in output.getvalue()


def test_duplicate_member_rows_are_rejected(treasurer, tmp_path, active_owes):
    path = write_csv(
        tmp_path,
        "Dana Owes,dana@example.com,1001,Active,$5.00\n"
        "Dana Owes,dana@example.com,,Active,$6.00\n",
    )

    with pytest.raises(CommandError, match="could not be matched"):
        run(path)


def test_expected_total_mismatch_stops_import(treasurer, standard_csv):
    with pytest.raises(CommandError, match="does not match expected"):
        run(standard_csv, "--expect-active-total", "999.00", "--commit")

    assert not LedgerEntry.objects.exists()


def test_expected_total_counts_only_active_members(treasurer, standard_csv):
    run(standard_csv, "--expect-active-total", "776.25", "--commit")

    assert LedgerEntry.objects.count() == 4


def test_zero_balance_rows_post_nothing(treasurer, tmp_path, active_owes):
    run(
        write_csv(tmp_path, "Dana Owes,dana@example.com,1001,Active,$0.00\n"),
        "--commit",
    )

    assert not LedgerEntry.objects.exists()


def test_actor_must_be_treasurer(tmp_path, active_owes):
    Member.objects.create_user(username="plain")
    path = write_csv(tmp_path, "Dana Owes,dana@example.com,1001,Active,$5.00\n")

    with pytest.raises(CommandError, match="Only treasurers"):
        call_command(
            "import_sheet_balances",
            path,
            "--date",
            CUTOVER.isoformat(),
            "--actor",
            "plain",
            "--commit",
        )


def test_commit_fails_cleanly_when_billing_disabled(treasurer, standard_csv):
    SiteConfiguration.objects.update(billing_app_enabled=False)

    assert "Dry run" in run(standard_csv)
    with pytest.raises(CommandError, match="Billing is disabled"):
        run(standard_csv, "--commit")
    assert not LedgerEntry.objects.exists()


def test_missing_inactive_member_is_created_on_commit(treasurer, tmp_path):
    path = write_csv(
        tmp_path, "Jacob Former,jacob@example.com,2001,Terminated,$35.00\n"
    )

    assert "NEW Inactive member" in run(path, "--create-missing-inactive")
    assert not Member.objects.filter(email="jacob@example.com").exists()

    run(path, "--create-missing-inactive", "--commit")

    created = Member.objects.get(email="jacob@example.com")
    assert created.username == "jacob.former"
    assert created.membership_status == "Inactive"
    assert created.SSA_member_number == "2001"
    assert not created.is_active
    assert not created.has_usable_password()
    assert get_balance(created.billing_ledger) == Decimal("0.00")
    assert created.billing_ledger.entries.count() == 2

    run(path, "--create-missing-inactive", "--commit")
    assert Member.objects.filter(email="jacob@example.com").count() == 1


def test_missing_active_member_is_never_created(treasurer, tmp_path):
    path = write_csv(tmp_path, "New Person,new@example.com,,Active,$5.00\n")

    with pytest.raises(CommandError, match="could not be matched"):
        run(path, "--create-missing-inactive", "--commit")

    assert not Member.objects.filter(email="new@example.com").exists()
