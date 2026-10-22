package main

import "errors"

type Tx struct {
	Account string
	Amount  int64
}

type Ledger struct {
	entries []Tx
}

func NewLedger() *Ledger {
	return &Ledger{}
}

// Apply validates a transaction and records it in the ledger.
func (l *Ledger) Apply(tx Tx) error {
	if err := validate(tx); err != nil {
		return err
	}
	l.append(tx)
	return nil
}

func (l *Ledger) append(tx Tx) {
	l.entries = append(l.entries, tx)
}

// Balance sums all entries for an account.
func (l *Ledger) Balance(account string) int64 {
	var total int64
	for _, e := range l.entries {
		if e.Account == account {
			total += e.Amount
		}
	}
	return total
}

// validate rejects transactions with no account or a zero amount.
func validate(tx Tx) error {
	if tx.Account == "" {
		return errors.New("missing account")
	}
	if tx.Amount == 0 {
		return errors.New("zero amount")
	}
	return nil
}
