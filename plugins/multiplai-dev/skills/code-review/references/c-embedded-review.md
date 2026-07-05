# C / Embedded Systems Code Review Reference

Load when reviewing C codebases, especially embedded/firmware (ESP32, STM32, Arduino, etc.).

## Issue Types

| Category | What to Check |
|----------|--------------|
| **Memory Safety** | Buffer overflows, use-after-free, double-free, null derefs, stack overflow |
| **Resource Management** | Leaked handles/descriptors, unclosed files, unfreed allocations |
| **Concurrency** | Race conditions, deadlocks, priority inversion, interrupt safety |
| **Integer Safety** | Overflow/underflow, signed/unsigned mismatch, truncation |
| **Peripheral & Hardware** | Register access volatility, timing, DMA safety, interrupt latency |
| **Power & Performance** | Sleep mode transitions, idle current, busy-wait vs interrupt-driven |

## Common Mistakes

| Mistake | Why It Matters | Fix |
|---------|---------------|-----|
| `strcpy()` / `sprintf()` | Buffer overflow | Use `strncpy()` / `snprintf()` with size |
| `gets()` | Always overflows | Use `fgets()` with buffer size |
| `malloc()` without NULL check | Null deref crash | Always check return value |
| `free()` without nulling pointer | Use-after-free | Set to `NULL` after free |
| `sizeof(ptr)` instead of `sizeof(*ptr)` | Gets pointer size, not element size | Use `sizeof(*ptr)` or `sizeof(type)` |
| Signed/unsigned comparison | Unexpected promotion behavior | Match types or cast explicitly |
| `printf(user_string)` | Format string vulnerability | Use `printf("%s", user_string)` |
| Missing `volatile` on hardware registers | Compiler optimizes away reads/writes | Mark all MMIO as `volatile` |
| Stack-allocated large arrays | Stack overflow on embedded | Use static or heap allocation |
| `#define` macros without parentheses | Operator precedence bugs | `#define SQUARE(x) ((x) * (x))` |
| Uninitialized variables | Undefined behavior | Initialize all variables at declaration |
| Ignoring return values of system calls | Silent failures | Check every return value |

## Memory Safety Checks

| Pattern | Risk | Verification |
|---------|------|-------------|
| Any `malloc`/`calloc`/`realloc` | Leak, null deref | Matching `free()` exists, NULL check present |
| Array access `arr[i]` | Out-of-bounds | `i` is bounds-checked before access |
| Pointer arithmetic | Buffer overflow | Result stays within allocated region |
| `memcpy`/`memmove` | Buffer overflow | Size parameter <= destination buffer size |
| String operations | Buffer overflow | Destination size passed and respected |
| Function returning pointer to local | Dangling pointer | Return heap-allocated or static |
| Casting between pointer types | Alignment issues | Check alignment requirements |

## Embedded-Specific Checks

### Interrupt Safety

| Issue | Fix |
|-------|-----|
| Non-atomic read/write of multi-byte variable shared with ISR | Use `volatile` + disable interrupts during access, or atomic types |
| `malloc()`/`printf()` inside ISR | Not reentrant — undefined behavior | Use pre-allocated buffers, set flags for main loop |
| Long ISR execution time | Blocks other interrupts, timing violations | Minimal ISR: set flag, defer work to main loop |
| Missing `volatile` on ISR-shared variables | Compiler caches in register, misses updates | Always `volatile` for ISR-shared data |
| Shared peripheral access without mutex | Corruption when multiple tasks access | Use mutex/semaphore (RTOS) or disable interrupts |

### RTOS-Specific (FreeRTOS, Zephyr, etc.)

| Issue | Fix |
|-------|-----|
| Stack size too small for task | Stack overflow (often silent corruption) | Measure with `uxTaskGetStackHighWaterMark()`, add margin |
| Priority inversion | High-priority task blocked by low-priority | Use priority inheritance mutexes |
| Blocking call in critical section | Deadlock | Never block while holding mutex |
| `vTaskDelay(0)` expecting yield | May not yield on all ports | Use `taskYIELD()` explicitly |
| Dynamic allocation after startup | Fragmentation, non-deterministic timing | Pre-allocate all buffers, use static allocation |

### Hardware Interface

| Issue | Fix |
|-------|-----|
| Direct register write without read-modify-write | Clobbers other bits in register | Read → mask → modify → write back |
| Missing memory barriers after DMA | CPU cache stale | Use `__DSB()` / `__DMB()` or cache invalidation |
| Polling in main loop for time-critical events | Missed events, wasted power | Use interrupts |
| Hardcoded magic numbers for register addresses | Unmaintainable | Use vendor HAL defines or create named constants |
| No timeout on hardware wait loops | Hangs forever if peripheral fails | Add timeout counter |

### Power Management

| Issue | Fix |
|-------|-----|
| Busy-wait loops (`while(!ready) {}`) | Wastes power, blocks CPU | Use interrupts + sleep, or RTOS event flags |
| Peripherals left enabled when unused | Unnecessary current draw | Disable clocks/peripherals when not in use |
| Missing deep sleep support | Battery drain | Implement sleep modes for idle periods |
| Frequent wake/sleep cycling | Transition current spikes | Batch operations, extend sleep periods |

## Integer Safety

| Pattern | Risk | Fix |
|---------|------|-----|
| `uint8_t a = 200; a + 100` | Wraps to 44 | Check before arithmetic or use larger type |
| `int a = -1; if (a < sizeof(buf))` | `-1` promoted to huge unsigned | Cast explicitly: `(int)sizeof(buf)` |
| `size_t` used for signed operations | Underflow to huge value | Use `ssize_t` or `ptrdiff_t` for signed |
| Bit shift by >= type width | Undefined behavior | Guard: `if (shift < 32)` |
| Multiplication overflow | Silent wrap, buffer calc wrong | Use overflow-safe math: check before multiply |

## MISRA-C Key Rules (Subset)

For safety-critical code, check these high-impact MISRA-C rules:

| Rule | Description |
|------|-------------|
| No `goto` | Use structured control flow |
| No recursion | Stack depth must be deterministic |
| No dynamic allocation after init | `malloc`/`free` only during startup |
| No implicit type conversions | Explicit casts for all narrowing |
| All `switch` cases have `break` or `/* fall through */` comment | Prevent accidental fallthrough |
| All functions have explicit return type | No implicit `int` |
| No pointer arithmetic beyond array bounds | Bounds-check all access |

## Do NOT Flag (C/Embedded)

- `goto` for error cleanup in resource-heavy functions (common C pattern, not a bug)
- Magic numbers in hardware register manipulation (when vendor-defined constants don't exist)
- Global variables for ISR-shared state (standard embedded pattern when properly `volatile`)
- `#pragma` directives for compiler-specific optimizations
- Assembly blocks (`__asm`) for hardware-specific operations
- `static` functions in `.c` files (proper encapsulation, not "hidden" code)
- Macro-heavy code in hardware abstraction layers (necessary for portability)
- Fixed-size buffers instead of dynamic allocation (intentional in embedded)
- Polling loops in initialization code (hardware needs time to stabilize)
