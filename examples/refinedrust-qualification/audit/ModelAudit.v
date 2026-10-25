From VKitLedger Require Model.

Definition checked_model_lemmas :=
  (@VKitLedger.Model.used_empty, @VKitLedger.Model.used_insert,
   @VKitLedger.Model.used_delete, @VKitLedger.Model.init_valid,
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
   @VKitLedger.Model.reserve_batch_too_large).

Set Printing Fully Qualified.
Unset Printing Notations.
Set Printing Width 1000000.
Redirect "../ledger/model-assumptions" Print Assumptions checked_model_lemmas.
