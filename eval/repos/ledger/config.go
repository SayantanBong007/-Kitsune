package main

import "os"

type Config struct {
	Addr   string
	DBPath string
}

// LoadConfig reads settings from the environment.
func LoadConfig() Config {
	return Config{Addr: os.Getenv("ADDR"), DBPath: os.Getenv("DB_PATH")}
}
