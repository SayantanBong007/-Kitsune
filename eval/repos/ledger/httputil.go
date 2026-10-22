package main

import (
	"encoding/json"
	"net/http"
)

func parseTx(r *http.Request) (Tx, error) {
	var tx Tx
	err := json.NewDecoder(r.Body).Decode(&tx)
	return tx, err
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, err error) {
	http.Error(w, err.Error(), http.StatusBadRequest)
}
