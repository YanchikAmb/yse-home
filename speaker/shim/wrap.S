/* Сгенерировано gen_wrap.py. Аргументы и адрес возврата сохраняются. */
.syntax unified
.arm

.macro WRAP id, name
.global \name
.type \name, %function
.align 2
\name:
    push {r0, r1, r2, r3, r4, r5, r6, r7, r8, r10, r11, lr}
    mov r1, sp
    mov r0, #\id
    bl tap_before
    cmp r0, #0
    beq \name\()_none
    str r0, [sp, #16]
    pop {r0, r1, r2, r3, r4}
    pop {r5, r6, r7, r8, r10, r11, lr}
    blx r4
    push {r0, r1, r2, r3}
    sub sp, sp, #8
    mov r1, sp
    add r0, sp, #8
    bl tap_after
    ldr r4, [sp]
    ldr r12, [sp, #4]
    add sp, sp, #8
    pop {r0, r1, r2, r3}
    mov lr, r12
    bx lr
\name\()_none:
    pop {r0, r1, r2, r3, r4, r5, r6, r7, r8, r10, r11, lr}
    mov r0, #0
    bx lr
.endm

WRAP 0, change_enableable_spotter_component_state

WRAP 1, spotter_audio_enrollment_data_free

WRAP 2, spotter_change_phrase_state

WRAP 3, spotter_conf_free

WRAP 4, spotter_conf_native_sample_rate

WRAP 5, spotter_conf_read

WRAP 6, spotter_conf_set_custom_phrase

WRAP 7, spotter_create_from_multi_conf

WRAP 8, spotter_destroy

WRAP 9, spotter_error_free

WRAP 10, spotter_error_get_func_name

WRAP 11, spotter_error_get_message

WRAP 12, spotter_error_get_type

WRAP 13, spotter_get_audio_enrollment_data

WRAP 14, spotter_get_current_activation_info_for_online_validation

WRAP 15, spotter_get_current_activation_metainfo

WRAP 16, spotter_get_current_channel_id

WRAP 17, spotter_get_current_component_id

WRAP 18, spotter_get_current_phrase

WRAP 19, spotter_get_current_phrase_id

WRAP 20, spotter_get_current_subthreshold_activation_priority

WRAP 21, spotter_get_local_counteractivation_score

WRAP 22, spotter_get_logs_for_component

WRAP 23, spotter_get_next_event

WRAP 24, spotter_get_next_event_seamless

WRAP 25, spotter_get_unhandled_nbytes

WRAP 26, spotter_multi_conf_attach_spotter_conf

WRAP 27, spotter_multi_conf_create

WRAP 28, spotter_multi_conf_detach_component

WRAP 29, spotter_multi_conf_free

WRAP 30, spotter_multi_conf_get_local_counteractivation_model_name_by_idx

WRAP 31, spotter_multi_conf_get_local_counteractivation_models_count

WRAP 32, spotter_multiconf_remove_audio_enrollment

WRAP 33, spotter_multiconf_set_audio_enrollment

WRAP 34, spotter_required_channel_count_from_dirname

WRAP 35, spotter_required_channel_count_to_submit_to_server_from_dirname

WRAP 36, spotter_reset_component

WRAP 37, spotter_reset_logs_for_component

WRAP 38, spotter_reset_timings

WRAP 39, spotter_submit_phoneme

WRAP 40, spotter_submit_phoneme_timing

WRAP 41, spotter_submit_raw_data

WRAP 42, spotter_submit_raw_data_seamless

WRAP 43, spotter_submit_word

WRAP 44, spotter_update_metainfo

WRAP 45, spotter_version_from_dirname

