// Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
// SPDX-License-Identifier: Apache-2.0

// Package rules compiles configured filter expressions and evaluates them
// against a log record's attributes.
package rules

import (
	"fmt"
	"sort"
	"strings"

	"github.com/expr-lang/expr"
	"github.com/expr-lang/expr/ast"
	"github.com/expr-lang/expr/parser"
	"github.com/expr-lang/expr/vm"

	"oxp-event-exporter/internal/config"
)

// kind classifies a rule so Matches can take a cheap direct path for the
// common shapes instead of always running the general expression engine.
type kind int

const (
	// kindGeneral covers every expression that isn't one of the recognized
	// shapes below; it's evaluated with the expr VM.
	kindGeneral kind = iota
	// kindTypeEquals is `type == "literal"`.
	kindTypeEquals
	// kindTypePrefix is `type startsWith "literal"`.
	kindTypePrefix
)

// Rule is a compiled, ready-to-evaluate notification rule.
type Rule struct {
	// Number is the rule's 1-based position in the configured list, used
	// only to identify the rule in logs and error messages.
	Number      int
	CallbackURL string

	kind    kind
	literal string      // the literal for kindTypeEquals/kindTypePrefix
	program *vm.Program // set only for kindGeneral
}

// Compile compiles every configured rule's filter expression. Rules shaped
// as a direct comparison on the "type" field (`type == "..."` or
// `type startsWith "..."`) are recognized up front so Matches can resolve
// them with a plain map lookup instead of invoking the expression engine.
func Compile(configured []config.Rule) ([]Rule, error) {
	compiled := make([]Rule, 0, len(configured))
	for i, c := range configured {
		number := i + 1
		r := Rule{Number: number, CallbackURL: c.CallbackURL}

		if k, literal, ok := classify(c.Rule); ok {
			r.kind = k
			r.literal = literal
		} else {
			// "type" doubles as an attribute name in these rules (e.g.
			// tool.completed events) and as an expr-lang builtin; disable
			// the builtin so the bare identifier resolves to the attribute.
			//
			// Not every log record carries every attribute, and the config
			// file plainly dots into nested fields (e.g. data.agent.name)
			// rather than using expr-lang's "?." optional-chaining syntax.
			// optionalizeMemberAccess rewrites the parsed expression so
			// every such path is nil-safe regardless, matching the "false
			// when an intermediate field is absent" contract documented in
			// config.yaml.
			program, err := expr.Compile(c.Rule, expr.DisableBuiltin("type"), expr.Patch(optionalizeMemberAccess{}))
			if err != nil {
				return nil, fmt.Errorf("rule %d: compile %q: %w", number, c.Rule, err)
			}
			r.kind = kindGeneral
			r.program = program
		}

		compiled = append(compiled, r)
	}
	return compiled, nil
}

// optionalizeMemberAccess is an expr.Patch visitor that turns every member
// access in a parsed expression (e.g. the `.edge` and `.kind` steps in
// `data.edge.kind`) into the equivalent of expr-lang's "?." optional
// chaining: a nil-safe lookup that short-circuits to nil instead of
// panicking when an intermediate value is nil. Combined with wrapping the
// outermost member access of a chain in a ChainNode (which is what actually
// enables the short-circuiting at runtime; Optional alone is not enough),
// this lets config rules use plain "." throughout while still tolerating
// records that don't carry every attribute.
type optionalizeMemberAccess struct{}

func (optionalizeMemberAccess) Visit(node *ast.Node) {
	switch n := (*node).(type) {
	case *ast.MemberNode:
		n.Optional = true
	case *ast.BinaryNode:
		chainIfMember(&n.Left)
		chainIfMember(&n.Right)
	case *ast.UnaryNode:
		chainIfMember(&n.Node)
	}
}

// chainIfMember wraps *node in a ChainNode if it's a member access, so the
// optional chaining set up by Visit above actually takes effect at runtime.
func chainIfMember(node *ast.Node) {
	if _, ok := (*node).(*ast.MemberNode); ok {
		ast.Patch(node, &ast.ChainNode{Node: *node})
	}
}

// classify recognizes `type == "literal"` and `type startsWith "literal"`,
// the shapes every rule in practice filters on first.
func classify(rule string) (k kind, literal string, ok bool) {
	tree, err := parser.Parse(rule)
	if err != nil {
		return kindGeneral, "", false
	}

	bin, isBinary := tree.Node.(*ast.BinaryNode)
	if !isBinary {
		return kindGeneral, "", false
	}

	ident, isIdent := bin.Left.(*ast.IdentifierNode)
	if !isIdent || ident.Value != "type" {
		return kindGeneral, "", false
	}

	str, isString := bin.Right.(*ast.StringNode)
	if !isString {
		return kindGeneral, "", false
	}

	switch bin.Operator {
	case "==":
		return kindTypeEquals, str.Value, true
	case "startsWith":
		return kindTypePrefix, str.Value, true
	default:
		return kindGeneral, "", false
	}
}

// Matches evaluates the rule's filter expression against attrs. For rules
// recognized by classify, this is a direct map lookup and string comparison;
// everything else runs through the expr VM.
func (r Rule) Matches(attrs map[string]any) (bool, error) {
	switch r.kind {
	case kindTypeEquals:
		typeValue, _ := attrs["type"].(string)
		return typeValue == r.literal, nil
	case kindTypePrefix:
		typeValue, _ := attrs["type"].(string)
		return strings.HasPrefix(typeValue, r.literal), nil
	default:
		out, err := expr.Run(r.program, expandDottedKeys(attrs))
		if err != nil {
			return false, err
		}
		matched, ok := out.(bool)
		if !ok {
			return false, fmt.Errorf("rule %d: expression did not evaluate to a boolean (got %T)", r.Number, out)
		}
		return matched, nil
	}
}

// expandDottedKeys makes flat OTLP keys readable as nested paths without
// changing the caller's map or replacing any explicitly nested values.
func expandDottedKeys(attrs map[string]any) map[string]any {
	expanded := make(map[string]any, len(attrs))
	var dottedKeys []string
	for key, value := range attrs {
		expanded[key] = value
		if strings.Contains(key, ".") {
			dottedKeys = append(dottedKeys, key)
		}
	}
	sort.Strings(dottedKeys)
	for _, key := range dottedKeys {
		parts := strings.Split(key, ".")
		current := expanded
		for index, part := range parts {
			if part == "" {
				break
			}
			if index == len(parts)-1 {
				if _, exists := current[part]; !exists {
					current[part] = attrs[key]
				}
				break
			}
			child, exists := current[part]
			if !exists {
				child = map[string]any{}
			}
			nested, ok := child.(map[string]any)
			if !ok {
				break
			}
			copyOfNested := make(map[string]any, len(nested)+1)
			for name, value := range nested {
				copyOfNested[name] = value
			}
			current[part] = copyOfNested
			current = copyOfNested
		}
	}
	return expanded
}
