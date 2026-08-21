import { parseISO } from "date-fns"

/**
 * parseISO for a timestamp the API schema declares nullable.
 *
 * date-fns 4 throws on null/undefined where 2.x returned Invalid Date. Callers
 * here feed the result to chart maths and groupBy keys that already handled
 * Invalid Date (NaN durations, an "Invalid Date" bucket), so reproduce it.
 */
export function parseISOOrInvalid(value) {
  return value ? parseISO(value) : new Date(NaN)
}
