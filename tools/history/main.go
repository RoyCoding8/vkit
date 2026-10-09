package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strconv"
	"time"
	"unicode/utf8"

	"github.com/anishathalye/porcupine"
)

const (
	protocolVersion = 1
	backendVersion  = "1.3.1"
	maxRequestBytes = 1_048_576
	maxOperations   = 1_000
	maxIDChars      = 128
)

type request struct {
	SchemaVersion *int            `json:"schema_version"`
	Operations    json.RawMessage `json:"operations"`
}

type requestOperation struct {
	ID       string          `json:"id"`
	ClientID int             `json:"client_id"`
	Call     int64           `json:"call"`
	Return   int64           `json:"return"`
	Input    json.RawMessage `json:"input"`
	Output   json.RawMessage `json:"output"`
}

type operationInput struct {
	Op    string `json:"op"`
	Value *int64 `json:"value"`
}

type modelOperation struct {
	Op    string
	Value *int64
}

type response struct {
	ProtocolVersion int      `json:"protocol_version"`
	BackendVersion  string   `json:"backend_version"`
	Status          string   `json:"status"`
	Linearization   []string `json:"linearization"`
}

func main() {
	if err := run(os.Args[1:], os.Stdin, os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
}

func run(args []string, input io.Reader, output io.Writer) error {
	if len(args) != 4 || args[0] != "--model" || args[2] != "--timeout-seconds" {
		return errors.New("expected --model register|queue --timeout-seconds N")
	}
	modelName := args[1]
	if modelName != "register" && modelName != "queue" {
		return errors.New("unsupported model")
	}
	timeoutSeconds, err := strconv.Atoi(args[3])
	if err != nil || timeoutSeconds < 1 || timeoutSeconds > 60 {
		return errors.New("timeout-seconds must be an integer in [1, 60]")
	}

	requestBytes, err := io.ReadAll(io.LimitReader(input, maxRequestBytes+1))
	if err != nil {
		return fmt.Errorf("read request: %w", err)
	}
	if len(requestBytes) > maxRequestBytes {
		return errors.New("request exceeds byte limit")
	}
	decoder := json.NewDecoder(bytes.NewReader(requestBytes))
	decoder.DisallowUnknownFields()
	var raw request
	if err := decoder.Decode(&raw); err != nil {
		return fmt.Errorf("invalid request: %w", err)
	}
	if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
		return errors.New("request must contain one JSON object")
	}
	if raw.SchemaVersion == nil || *raw.SchemaVersion != protocolVersion || len(raw.Operations) == 0 || raw.Operations[0] != '[' {
		return errors.New("unsupported request version or operation count")
	}
	var rawOperations []requestOperation
	if err := json.Unmarshal(raw.Operations, &rawOperations); err != nil {
		return fmt.Errorf("invalid operations: %w", err)
	}
	if len(rawOperations) > maxOperations {
		return errors.New("operation count exceeds limit")
	}

	operations := make([]porcupine.Operation, 0, len(rawOperations))
	ids := make(map[string]struct{}, len(rawOperations))
	for i, item := range rawOperations {
		if item.ID == "" || utf8.RuneCountInString(item.ID) > maxIDChars || item.ClientID < 0 || item.ClientID > 2_147_483_647 || item.Call >= item.Return {
			return fmt.Errorf("invalid operation at index %d", i)
		}
		if _, exists := ids[item.ID]; exists {
			return fmt.Errorf("duplicate operation id %q", item.ID)
		}
		ids[item.ID] = struct{}{}
		var in operationInput
		if err := strictUnmarshal(item.Input, &in); err != nil {
			return fmt.Errorf("invalid operation input at index %d", i)
		}
		var out *int64
		if len(item.Output) == 0 {
			return fmt.Errorf("missing operation output at index %d", i)
		}
		if string(item.Output) != "null" {
			var value int64
			if err := json.Unmarshal(item.Output, &value); err != nil {
				return fmt.Errorf("invalid operation output at index %d", i)
			}
			out = &value
		}
		if err := validateOperation(modelName, in, out); err != nil {
			return fmt.Errorf("invalid operation at index %d: %w", i, err)
		}
		operations = append(operations, porcupine.Operation{
			ClientId: item.ClientID,
			Input:    modelOperation{Op: in.Op, Value: in.Value},
			Call:     item.Call,
			Output:   out,
			Return:   item.Return,
			Metadata: item.ID,
		})
	}

	check, info := porcupine.CheckOperationsVerbose(fixedModel(modelName), operations,
		time.Duration(timeoutSeconds)*time.Second)
	result := response{ProtocolVersion: protocolVersion, BackendVersion: backendVersion,
		Status: "UNKNOWN"}
	switch check {
	case porcupine.Ok:
		result.Status = "OK"
	case porcupine.Illegal:
		result.Status = "ILLEGAL"
	}
	if check == porcupine.Ok {
		if len(operations) == 0 {
			result.Linearization = []string{}
		} else {
			partials := info.PartialLinearizationsOperations()
			if len(partials) != 1 || len(partials[0]) != 1 {
				return errors.New("Porcupine did not produce one complete linearization")
			}
			for _, operation := range partials[0][0] {
				id, ok := operation.Metadata.(string)
				if !ok {
					return errors.New("Porcupine witness lost operation identity")
				}
				result.Linearization = append(result.Linearization, id)
			}
			if len(result.Linearization) != len(operations) {
				return errors.New("Porcupine witness was incomplete")
			}
		}
	} else {
		result.Linearization = nil
	}
	encoder := json.NewEncoder(output)
	encoder.SetEscapeHTML(false)
	return encoder.Encode(result)
}

func strictUnmarshal(raw json.RawMessage, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
		return errors.New("trailing JSON")
	}
	return nil
}

func validateOperation(modelName string, input operationInput, output *int64) error {
	if input.Op == "" {
		return errors.New("operation name is required")
	}
	switch modelName {
	case "register":
		switch input.Op {
		case "read":
			if input.Value != nil || output == nil {
				return errors.New("read requires no input value and an integer output")
			}
		case "write":
			if input.Value == nil || output != nil {
				return errors.New("write requires an integer input value and null output")
			}
		default:
			return errors.New("unsupported register operation")
		}
	case "queue":
		switch input.Op {
		case "enqueue":
			if input.Value == nil || output != nil {
				return errors.New("enqueue requires an integer input value and null output")
			}
		case "dequeue":
			if input.Value != nil {
				return errors.New("dequeue takes no input value")
			}
		default:
			return errors.New("unsupported queue operation")
		}
	}
	return nil
}

func fixedModel(name string) porcupine.Model {
	if name == "register" {
		return porcupine.Model{
			Init: func() interface{} { return int64(0) },
			StepContext: func(ctx context.Context, state, input, output interface{}) (bool, interface{}) {
				if ctx.Err() != nil {
					return false, state
				}
				current := state.(int64)
				operation := input.(modelOperation)
				observed := output.(*int64)
				switch operation.Op {
				case "read":
					return *observed == current, current
				case "write":
					return true, *operation.Value
				default:
					return false, current
				}
			},
			Equal: func(left, right interface{}) bool { return left.(int64) == right.(int64) },
		}
	}
	return porcupine.Model{
		Init: func() interface{} { return []int64{} },
		StepContext: func(ctx context.Context, state, input, output interface{}) (bool, interface{}) {
			if ctx.Err() != nil {
				return false, state
			}
			queue := state.([]int64)
			operation := input.(modelOperation)
			observed := output.(*int64)
			switch operation.Op {
			case "enqueue":
				next := append(append([]int64(nil), queue...), *operation.Value)
				return true, next
			case "dequeue":
				if len(queue) == 0 {
					return observed == nil, queue
				}
				if observed == nil || *observed != queue[0] {
					return false, queue
				}
				next := append([]int64(nil), queue[1:]...)
				return true, next
			default:
				return false, queue
			}
		},
		Equal: func(left, right interface{}) bool {
			a, b := left.([]int64), right.([]int64)
			if len(a) != len(b) {
				return false
			}
			for i := range a {
				if a[i] != b[i] {
					return false
				}
			}
			return true
		},
	}
}
