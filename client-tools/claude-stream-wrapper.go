// Native Windows launcher for VS Code's supported claudeProcessWrapper setting.
// Windows cannot execute the Unix launcher or a .cmd through the SDK's spawn.
package main

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"syscall"
)

func streamingArgs(args []string) []string {
	streaming, partial, explicitEffort := false, false, false
	for i, arg := range args {
		if arg == "--include-partial-messages" {
			partial = true
		}
		if arg == "--effort" || strings.HasPrefix(arg, "--effort=") {
			explicitEffort = true
		}
		if arg == "--output-format=stream-json" || (arg == "--output-format" && i+1 < len(args) && args[i+1] == "stream-json") {
			streaming = true
		}
	}
	if streaming && !partial {
		args = append(append([]string{}, args...), "--include-partial-messages")
	}
	if streaming && !explicitEffort && os.Getenv("AERISUN_CLAUDE_INITIAL_EFFORT") == "max" {
		args = append(append([]string{}, args...), "--effort", "max")
	}
	return args
}

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "Expected the bundled Claude executable as the first argument")
		os.Exit(2)
	}
	if err := protectChildProcesses(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	cmd := exec.Command(os.Args[1], streamingArgs(os.Args[2:])...)
	for _, value := range os.Environ() {
		if !strings.HasPrefix(value, "AERISUN_CLAUDE_INITIAL_EFFORT=") {
			cmd.Env = append(cmd.Env, value)
		}
	}
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	if err := cmd.Start(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	signals := make(chan os.Signal, 1)
	done := make(chan struct{})
	signal.Notify(signals, os.Interrupt, syscall.SIGTERM)
	go func() {
		select {
		case sig := <-signals:
			if err := cmd.Process.Signal(sig); err != nil {
				_ = cmd.Process.Kill()
			}
		case <-done:
		}
	}()
	err := cmd.Wait()
	close(done)
	signal.Stop(signals)
	if err != nil {
		var exitError *exec.ExitError
		if errors.As(err, &exitError) {
			code := exitError.ExitCode()
			if code < 0 {
				code = 143
			}
			os.Exit(code)
		}
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
