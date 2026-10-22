package main

import "net/http"

// handleTx records a transaction posted as JSON.
func (s *Server) handleTx(w http.ResponseWriter, r *http.Request) {
	tx, err := parseTx(r)
	if err != nil {
		writeError(w, err)
		return
	}
	if err := s.ledger.Apply(tx); err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, tx)
}

func (s *Server) handleBalance(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, s.ledger.Balance(r.URL.Query().Get("account")))
}
