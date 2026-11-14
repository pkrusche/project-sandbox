# TODO - outstanding items for next release

- `docs/runtime.md` step 2 of "End-to-End Flow" (line 11) says agent
  credentials are staged "under `/tmp`". CLI runs now stage them in private
  session inputs under the state directory
  (`$XDG_STATE_HOME/project-sandbox/session-inputs/<id>`, from
  `session_inputs.allocate()`); only the persistent devcontainer staging is
  under `/tmp/project-sandbox-<uid>/...`, which line 105 of the same page states
  correctly. Reword step 2 to match and link `security.md#private-session-inputs`.
  Found while checking Colima mount visibility; not part of `add-colima-runtime`.
