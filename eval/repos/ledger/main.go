package main

func main() {
	cfg := LoadConfig()
	srv := NewServer(cfg)
	srv.Start()
}
