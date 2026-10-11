package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

func TestTransactionStepReadsItsWritesAndCommitsAtomically(t *testing.T) {
	state := map[string]int64{"x": 4}
	valueX, valueY := int64(9), int64(2)
	accepted, next := transactionStep(state, []transactionAction{
		{Op: "write", Key: "x", Value: &valueX},
		{Op: "read", Key: "x"},
		{Op: "write", Key: "y", Value: &valueY},
		{Op: "read", Key: "y"},
	}, []int64{9, 2})
	if !accepted {
		t.Fatal("transaction with matching read-your-writes observations was rejected")
	}
	if state["x"] != 4 || len(state) != 1 {
		t.Fatalf("accepted transaction mutated prior state: %#v", state)
	}
	committed := next.(map[string]int64)
	if committed["x"] != 9 || committed["y"] != 2 {
		t.Fatalf("unexpected committed state: %#v", committed)
	}
}

func TestFailedTransactionDoesNotLeakEarlierWrites(t *testing.T) {
	state := map[string]int64{"x": 4}
	valueX, valueY := int64(9), int64(2)
	accepted, next := transactionStep(state, []transactionAction{
		{Op: "write", Key: "x", Value: &valueX},
		{Op: "write", Key: "y", Value: &valueY},
		{Op: "read", Key: "x"},
	}, []int64{4})
	if accepted {
		t.Fatal("transaction with a stale read was accepted")
	}
	if state["x"] != 4 || len(state) != 1 || next.(map[string]int64)["x"] != 4 {
		t.Fatalf("failed transaction changed state: prior=%#v next=%#v", state, next)
	}
}

func TestRunAcceptsTransactionAndKeepsRegisterAndQueueModels(t *testing.T) {
	cases := []struct {
		name  string
		model string
		body  string
		want  []string
	}{
		{"transaction", "transaction", `{"schema_version":1,"operations":[{"id":"tx","client_id":0,"call":1,"return":2,"input":{"actions":[{"op":"write","key":"x","value":7},{"op":"read","key":"x"}]},"output":[7]}]}`, []string{"tx"}},
		{"register", "register", `{"schema_version":1,"operations":[{"id":"write","client_id":0,"call":1,"return":2,"input":{"op":"write","value":7},"output":null},{"id":"read","client_id":0,"call":3,"return":4,"input":{"op":"read"},"output":7}]}`, []string{"write", "read"}},
		{"queue", "queue", `{"schema_version":1,"operations":[{"id":"enqueue","client_id":0,"call":1,"return":2,"input":{"op":"enqueue","value":7},"output":null},{"id":"dequeue","client_id":0,"call":3,"return":4,"input":{"op":"dequeue"},"output":7}]}`, []string{"enqueue", "dequeue"}},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			var output bytes.Buffer
			args := []string{"--model", test.model, "--timeout-seconds", "2"}
			if err := run(args, strings.NewReader(test.body), &output); err != nil {
				t.Fatalf("run returned an error: %v", err)
			}
			var result response
			if err := json.Unmarshal(output.Bytes(), &result); err != nil {
				t.Fatalf("invalid worker response %q: %v", output.String(), err)
			}
			if result.Status != "OK" || strings.Join(result.Linearization, ",") != strings.Join(test.want, ",") {
				t.Fatalf("unexpected result: %+v", result)
			}
		})
	}
}

func TestRunRejectsMalformedTransactionShapes(t *testing.T) {
	badRequests := []string{
		`{"schema_version":1,"operations":[{"id":"tx","client_id":0,"call":1,"return":2,"input":{"actions":[]},"output":[]}]}`,
		`{"schema_version":1,"operations":[{"id":"tx","client_id":0,"call":1,"return":2,"input":{"actions":[{"op":"read","key":"x"}]},"output":[]}]}`,
		`{"schema_version":1,"operations":[{"id":"tx","client_id":0,"call":1,"return":2,"input":{"actions":[{"op":"write","key":"x"}]},"output":[]}]}`,
	}
	for _, body := range badRequests {
		var output bytes.Buffer
		if err := run([]string{"--model", "transaction", "--timeout-seconds", "2"},
			strings.NewReader(body), &output); err == nil {
			t.Fatalf("accepted malformed transaction request %s", body)
		}
	}
}
