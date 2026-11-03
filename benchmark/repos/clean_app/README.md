# clean_app

Benchmark fixture: a small document service with **no planted issues**. It exists
so the harness can measure false positives — every finding reported here is a
false positive by construction.

Test data only: never executed, imported or handed to a shell.

What makes it clean:

- no hardcoded credentials; configuration comes from the environment
- every query is parameterised and scoped by `owner_id`
- every route obtains the caller through `auth.current_user`
- imports flow one way (`routes` -> `service` -> `store`), so there is no cycle
- dependencies are permissive (BSD and Apache-2.0) and pinned to versions with
  no published advisory
