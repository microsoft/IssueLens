# Duplicate Detection Criteria

Use these examples to apply the duplicate threshold consistently.

## Primary Matches

At least one primary criterion must match.

### Identical Error Signature

Match exact error codes, exception types with a specific location, or distinctive
message text.

```text
Issue A: ENOENT: no such file or directory, open '/tmp/cache.json'
Issue B: Error: ENOENT: no such file or directory, open '/tmp/cache.json'
Result: Match; the code and path are identical.
```

Similar concepts with different details are not a primary match:

```text
Issue A: Connection timeout after 30 seconds
Issue B: Request timeout with no response
Result: No match without stronger technical evidence.
```

### Same Stack Signature

Compare the top three functions and files while ignoring line numbers.

```text
Issue A: Parser.parse -> Compiler.compile -> build
Issue B: Parser.parse -> Compiler.compile -> build
Result: Match even when source line numbers differ.
```

The same exception class in different functions is not a match.

### Identical Reproduction Steps

The action sequence, relevant inputs, and configuration state must align. Minor
wording differences are acceptable; missing or different triggers are not.

## Supporting Matches

### Same Component or Area

Look for matching area/component labels, feature names, or mentioned paths.
Area similarity alone never establishes a duplicate.

### Same Environment

Compare only environment details relevant to the defect: operating system,
application version, runtime, browser, architecture, or hardware.

### Same Symptom

Distinguish precise behavior such as crash, freeze, corruption, wrong output,
or performance degradation. Two different symptoms in one feature are usually
separate reports.

### Same Trigger

Compare the input, configuration, size threshold, permissions, timing, and
action sequence that provoke the defect.

### Cross-Reference

An explicit link or `duplicate of #N` statement is strong evidence, but verify
that the linked issues describe matching technical behavior before assigning
High confidence.

## Edge Cases

### Generic Errors

`Connection refused`, `timeout`, `permission denied`, `out of memory`, `file not
found`, and `invalid argument` have many causes. Require at least three strong
supporting matches.

### Different Versions

When versions produce different stack traces, messages, or behavior, classify
the reports as related unless the issue content demonstrates the same defect.

### Same Root Cause, Different Manifestation

Reports can share an implementation root cause while documenting distinct user
scenarios. Keep them separate when users would search for different symptoms or
follow different reproduction steps.

## Not Duplicates

- Same feature, different problem: slow search versus incorrect search results.
- Same exception class, different failing location.
- Vague reports with no comparable technical details.
- Same workaround applied to unrelated failures.
- Similar titles without matching error, stack, or reproduction evidence.