# Language-Specific Security Reference

Load this file when performing deep security review and the language needs detailed coverage beyond the quick-reference table in SKILL.md.

## JavaScript / TypeScript

**Main Risks:** Prototype pollution, XSS, eval injection

```javascript
// UNSAFE: Prototype pollution
Object.assign(target, userInput)
// SAFE: Use null prototype or validate keys
Object.assign(Object.create(null), validated)

// UNSAFE: XSS
element.innerHTML = userInput
// SAFE: Use textContent or sanitize
element.textContent = userInput

// UNSAFE: eval injection
eval(userCode)
// SAFE: Never use eval with user input
```

**Watch for:** `eval()`, `innerHTML`, `document.write()`, `__proto__`, `constructor.prototype`, `Function()` constructor, `setTimeout/setInterval` with string args, `postMessage` without origin checks, `window.open` with user URLs

---

## Python

**Main Risks:** Pickle deserialization, format string injection, shell injection

```python
# UNSAFE: Pickle RCE
pickle.loads(user_data)
# SAFE: Use JSON
json.loads(user_data)

# UNSAFE: Shell injection
os.system(f"convert {filename} output.png")
# SAFE: No shell
subprocess.run(["convert", filename, "output.png"], shell=False)

# UNSAFE: Format string injection
query = "SELECT * FROM users WHERE name = '%s'" % user_input
# SAFE: Parameterized
cursor.execute("SELECT * FROM users WHERE name = %s", (user_input,))
```

**Watch for:** `pickle`, `eval()`, `exec()`, `os.system()`, `subprocess(shell=True)`, `yaml.load()` (not `safe_load`), `__import__()`, `compile()`, `marshal.loads()`, `shelve`, `tempfile` without proper permissions

---

## Java

**Main Risks:** Deserialization RCE, XXE, JNDI injection

```java
// UNSAFE: Arbitrary deserialization
ObjectInputStream ois = new ObjectInputStream(userStream);
Object obj = ois.readObject();

// SAFE: Use JSON with type restrictions
ObjectMapper mapper = new ObjectMapper();
mapper.readValue(json, SafeClass.class);

// UNSAFE: XXE
DocumentBuilder db = dbf.newDocumentBuilder();
Document doc = db.parse(userXml);

// SAFE: Disable external entities
dbf.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
```

**Watch for:** `ObjectInputStream`, `Runtime.exec()`, `ProcessBuilder`, XML parsers without XXE protection, JNDI lookups (`InitialContext.lookup()`), Spring Expression Language (SpEL), `Class.forName()` with user input

---

## Go

**Main Risks:** Race conditions, template injection, slice bounds

```go
// UNSAFE: Race condition
go func() { counter++ }()
// SAFE: Use sync primitives
atomic.AddInt64(&counter, 1)

// UNSAFE: Template injection
template.HTML(userInput)
// SAFE: Let template auto-escape
{{.UserInput}}

// UNSAFE: SSRF
resp, _ := http.Get(userURL)
// SAFE: Validate URL scheme and host
```

**Watch for:** Goroutine data races (use `-race` flag), `template.HTML()`, `unsafe` package, unchecked slice access, `os/exec.Command` with user input, missing `defer rows.Close()`, error values silently ignored

---

## Ruby

**Main Risks:** Mass assignment, YAML deserialization, regex DoS

```ruby
# UNSAFE: Mass assignment
User.new(params[:user])
# SAFE: Strong parameters
User.new(params.require(:user).permit(:name, :email))

# UNSAFE: YAML RCE
YAML.load(user_input)
# SAFE: Use safe_load
YAML.safe_load(user_input)
```

**Watch for:** `YAML.load`, `Marshal.load`, `eval`, `send` with user input, `constantize` with user input, `permit!` (permits everything), ERB templates with user data, `system()` and backticks

---

## PHP

**Main Risks:** Type juggling, file inclusion, object injection

```php
// UNSAFE: Type juggling in auth
if ($password == $stored_hash) { ... }
// SAFE: Strict comparison
if (hash_equals($stored_hash, $password)) { ... }

// UNSAFE: File inclusion
include($_GET['page'] . '.php');
// SAFE: Allowlist
$allowed = ['home', 'about'];
include(in_array($page, $allowed) ? "$page.php" : 'home.php');
```

**Watch for:** `==` vs `===`, `include/require` with user input, `unserialize()`, `preg_replace` with `/e` modifier, `extract()`, `assert()`, `create_function()`, `$$` variable variables

---

## Rust

**Main Risks:** Unsafe blocks, FFI boundary issues, integer overflow in release

```rust
// CAUTION: Unsafe bypasses all safety
unsafe { ptr::read(user_ptr) }

// CAUTION: Release mode integer overflow wraps silently
let x: u8 = 255;
let y = x + 1; // 0 in release, panic in debug!
// SAFE: Use checked arithmetic
let y = x.checked_add(1).unwrap_or(255);
```

**Watch for:** `unsafe` blocks (every one is an audit target), FFI calls, integer overflow in release builds, `.unwrap()` on untrusted input, `std::mem::transmute`, `std::ptr` operations, `libc` calls

---

## C / C++

**Main Risks:** Buffer overflow, use-after-free, format string

```c
// UNSAFE: Buffer overflow
char buf[10]; strcpy(buf, userInput);
// SAFE: Bounds checking
strncpy(buf, userInput, sizeof(buf) - 1);
buf[sizeof(buf) - 1] = '\0';

// UNSAFE: Format string
printf(userInput);
// SAFE: Always use format specifier
printf("%s", userInput);
```

**Watch for:** `strcpy`, `sprintf`, `gets`, `scanf` without width, pointer arithmetic, manual `malloc`/`free`, double-free, use-after-free, integer overflow/underflow, signed/unsigned comparison, off-by-one in buffer ops

---

## Shell (Bash)

**Main Risks:** Command injection, word splitting, globbing

```bash
# UNSAFE: Unquoted variables
rm $user_file
# SAFE: Always quote
rm "$user_file"

# UNSAFE: eval
eval "$user_command"
# SAFE: Never eval user input

# Missing safety net
# SAFE: Always start scripts with
set -euo pipefail
```

**Watch for:** Unquoted variables, `eval`, backticks with user input, `$(...)` with user input, missing `set -euo pipefail`, `curl | bash` patterns, `chmod 777`, world-readable credential files

---

## Deep Analysis Mindset

For ANY language, think through these 10 dimensions:

1. **Memory model** — Managed vs manual? GC pauses exploitable?
2. **Type system** — Weak typing = type confusion attacks
3. **Serialization** — Every language has a dangerous deserializer
4. **Concurrency** — Race conditions specific to the threading model
5. **FFI boundaries** — Where type safety breaks down
6. **Standard library CVEs** — Historical vulns in common stdlib modules
7. **Package ecosystem** — Typosquatting, dependency confusion risks
8. **Build system** — Script injection during builds (npm postinstall, setup.py)
9. **Runtime behavior** — Debug vs release differences
10. **Error handling** — Does the language fail silently, loudly, or configurably?
