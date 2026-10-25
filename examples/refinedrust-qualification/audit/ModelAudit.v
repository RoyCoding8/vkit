From VKitLedger Require Model.

Definition checked_model_lemmas :=
  (@VKitLedger.Model.used_empty, @VKitLedger.Model.used_insert,
   @VKitLedger.Model.used_delete, @VKitLedger.Model.init_valid,
   @VKitLedger.Model.active_pairs_length, @VKitLedger.Model.slot_records_size,
   @VKitLedger.Model.used_list_to_map, @VKitLedger.Model.used_slot_records,
   @VKitLedger.Model.slot_view_size_bound, @VKitLedger.Model.slot_cache_matches_map,
   @VKitLedger.Model.reserve_valid, @VKitLedger.Model.release_valid,
   @VKitLedger.Model.reserve_rejected_unchanged,
   @VKitLedger.Model.release_rejected_unchanged,
   @VKitLedger.Model.reserve_sequence_valid,
   @VKitLedger.Model.reserve_batch_valid,
   @VKitLedger.Model.reserve_batch_rejected_unchanged,
   @VKitLedger.Model.reserve_success_accounting,
   @VKitLedger.Model.release_success_accounting,
   @VKitLedger.Model.reserve_sequence_first_failure,
   @VKitLedger.Model.reserve_batch_first_failure,
   @VKitLedger.Model.reserve_rejection_spec,
   @VKitLedger.Model.release_rejection_spec,
   @VKitLedger.Model.reserve_sequence_accounting,
   @VKitLedger.Model.reserve_batch_success_accounting,
   @VKitLedger.Model.reserve_batch_too_large,
   @VKitLedger.Model.perform_valid,
   @VKitLedger.Model.run_commands_correct).

Set Printing Fully Qualified.
Unset Printing Notations.
Set Printing Width 1000000.
Redirect "../ledger/model-assumptions" Print Assumptions checked_model_lemmas.
