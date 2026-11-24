"""Hierarchical budget ledger (fleet -> run -> task) in integer micro-dollars.

Spending follows reserve -> commit:

* ``reserve`` succeeds only if *every* account on the chain to the root has room, and then
  holds the amount on every one of them. This is the admission check: no model call starts
  unless its worst case is already paid for.
* ``commit`` settles a reservation at its actual cost. An actual cost above the reservation is
  recorded as ``overrun`` and freezes any account it pushes over its limit, so nothing further
  is admitted against it until an operator raises the limit.

Invariant (checked by the simulator): for every account,
``spent + reserved <= limit + overrun``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .errors import BudgetExceeded, FleetError


@dataclass
class Account:
    name: str
    limit: int
    parent: str | None
    spent: int = 0
    reserved: int = 0
    overrun: int = 0
    frozen: bool = False

    @property
    def available(self) -> int:
        return 0 if self.frozen else max(0, self.limit - self.spent - self.reserved)


@dataclass(frozen=True)
class Reservation:
    id: int
    account: str
    amount: int


class Ledger:
    def __init__(self) -> None:
        self._accounts: dict[str, Account] = {}
        self._open: dict[int, Reservation] = {}
        self._next_id = 1

    def open(self, name: str, limit: int, parent: str | None = None) -> None:
        if name in self._accounts:
            raise FleetError(f"account {name!r} already exists")
        if parent is not None and parent not in self._accounts:
            raise FleetError(f"parent account {parent!r} does not exist")
        if limit < 0:
            raise FleetError(f"account {name!r}: limit must be >= 0")
        self._accounts[name] = Account(name=name, limit=limit, parent=parent)

    def account(self, name: str) -> Account:
        return replace(self._accounts[name])

    def accounts(self) -> list[Account]:
        return [replace(a) for a in self._accounts.values()]

    def _chain(self, name: str) -> list[Account]:
        chain = []
        current: str | None = name
        while current is not None:
            account = self._accounts[current]
            chain.append(account)
            current = account.parent
        return chain

    def available(self, name: str) -> int:
        """Largest amount ``reserve(name, amount)`` would accept right now."""
        return min(a.available for a in self._chain(name))

    def reserve(self, name: str, amount: int) -> Reservation:
        if amount < 0:
            raise FleetError("reservation amount must be >= 0")
        chain = self._chain(name)
        for account in chain:
            if account.frozen or account.limit - account.spent - account.reserved < amount:
                raise BudgetExceeded(account.name, amount, account.available)
        for account in chain:
            account.reserved += amount
        reservation = Reservation(id=self._next_id, account=name, amount=amount)
        self._next_id += 1
        self._open[reservation.id] = reservation
        return reservation

    def commit(self, reservation: Reservation, actual: int) -> int:
        """Settle ``reservation`` at ``actual`` micro-USD. Returns the overrun (>= 0)."""
        if actual < 0:
            raise FleetError("actual cost must be >= 0")
        if self._open.pop(reservation.id, None) is None:
            raise FleetError(f"reservation {reservation.id} is not open")
        overrun = max(0, actual - reservation.amount)
        for account in self._chain(reservation.account):
            account.reserved -= reservation.amount
            account.spent += actual
            account.overrun += overrun
            if account.spent + account.reserved > account.limit:
                account.frozen = True
        return overrun

    def raise_limit(self, name: str, amount: int) -> None:
        if amount < 0:
            raise FleetError("limit increase must be >= 0")
        account = self._accounts[name]
        account.limit += amount
        if account.frozen and account.spent + account.reserved <= account.limit:
            account.frozen = False

    @property
    def open_reservations(self) -> int:
        return len(self._open)
