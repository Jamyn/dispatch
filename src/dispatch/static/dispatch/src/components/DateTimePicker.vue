<template>
  <v-card>
    <!-- TODO: use vuetify picker components -->
    <v-card-text>
      <v-text-field v-model="selectedDatetime" type="datetime-local" />
    </v-card-text>
  </v-card>
</template>
<script>
import { parseISO } from "date-fns"
import { format, formatInTimeZone, fromZonedTime } from "date-fns-tz"

export default {
  name: "DateTimePicker",

  inheritAttrs: false,

  props: {
    modelValue: {
      type: [Date, String],
      default: null,
    },
    label: {
      type: String,
      default: "",
    },
  },

  data() {
    return {
      display: false,
      selectedDatetime: null,
    }
  },

  mounted() {
    this.init()
  },

  computed: {
    formattedDatetime() {
      return this.selectedDatetime
        ? format(parseISO(this.selectedDatetime), "yyyy-MM-dd HH:mm")
        : ""
    },
  },

  methods: {
    init() {
      if (!this.modelValue) {
        return
      }
      let initDateTime

      if (this.modelValue instanceof Date) {
        initDateTime = this.modelValue
      } else if (typeof this.modelValue === "string") {
        initDateTime = parseISO(this.modelValue)
      }

      // modelValue is an instant (Z-suffixed string or Date), not a wall clock.
      // `format`'s timeZone option only applies to [xXOz] pattern tokens, so it is
      // inert on a pattern without one. formatInTimeZone converts unconditionally.
      this.selectedDatetime = formatInTimeZone(initDateTime, "UTC", "yyyy-MM-dd'T'HH:mm")
    },
    okHandler() {
      this.resetPicker()
      // The field holds a UTC wall clock; parseISO would read it as host-local.
      let isoString = fromZonedTime(this.selectedDatetime, "UTC").toISOString()
      this.$emit("update:modelValue", isoString)
    },
    clearHandler() {
      this.resetPicker()
      this.$emit("update:modelValue", null)
    },
    resetPicker() {
      this.display = false
    },
    showTimePicker() {
      this.activeTab = 1
    },
  },
  watch: {
    modelValue() {
      this.init()
    },
  },
}
</script>
