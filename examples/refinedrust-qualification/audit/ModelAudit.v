From VKitLedger Require Model.

Definition checked_model_lemmas :=
  (@VKitLedger.Model.used_empty, @VKitLedger.Model.used_insert,
   @VKitLedger.Model.used_delete, @VKitLedger.Model.init_valid).

Set Printing Fully Qualified.
Unset Printing Notations.
Set Printing Width 1000000.
Redirect "../ledger/model-assumptions" Print Assumptions checked_model_lemmas.
