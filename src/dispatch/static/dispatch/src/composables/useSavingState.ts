import { computed, ComputedRef } from "vue"
import { useStore } from "vuex"

// The slice this composable actually reads. `@/store/case` never existed, and
// useStore takes the state type, not a Store wrapped around it -- together
// those made the old annotation unresolvable and its state access unchecked.
interface SavingState {
  case_management: { selected: { saving: boolean } }
}

interface UseSavingStateReturns {
  saving: ComputedRef<boolean>
  setSaving: (value: boolean) => void
}

export function useSavingState(): UseSavingStateReturns {
  const store = useStore<SavingState>()

  const saving = computed(() => store.state.case_management.selected.saving)

  const setSaving = (value: boolean) => {
    store.commit("case_management/SET_SELECTED_SAVING", value)
  }

  return {
    saving,
    setSaving,
  }
}
