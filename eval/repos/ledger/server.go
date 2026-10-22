package main

import "net/http"

type Server struct {
	cfg    Config
	ledger *Ledger
}

// NewServer wires the HTTP server to a fresh ledger.
func NewServer(cfg Config) *Server {
	return &Server{cfg: cfg, ledger: NewLedger()}
}

// Start registers routes and serves HTTP.
func (s *Server) Start() error {
	s.routes()
	return http.ListenAndServe(s.cfg.Addr, nil)
}

func (s *Server) routes() {
	http.HandleFunc("/tx", s.handleTx)
	http.HandleFunc("/balance", s.handleBalance)
}
