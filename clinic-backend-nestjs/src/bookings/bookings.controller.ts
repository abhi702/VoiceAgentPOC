import {
  Body, Controller, Get, Headers, MessageEvent, Post, Query, Sse, UnauthorizedException,
} from "@nestjs/common";
import { IsDateString, IsIn, IsNotEmpty, IsString, IsUUID, MaxLength } from "class-validator";
import { timingSafeEqual } from "node:crypto";
import { Observable } from "rxjs";
import { BookingsService } from "./bookings.service";

class IdentityInput {
  @IsString() @IsNotEmpty() @MaxLength(120) patient_name!: string;
  @IsDateString({ strict: true }) patient_dob!: string;
}
class BookingInput extends IdentityInput {
  @IsString() @IsNotEmpty() @MaxLength(36) slot_id!: string;
  @IsUUID() call_id!: string;
  @IsUUID() idempotency_key!: string;
}
class PrescriptionInput extends IdentityInput {
  @IsString() @IsNotEmpty() @MaxLength(120) medication!: string;
  @IsUUID() call_id!: string;
  @IsUUID() idempotency_key!: string;
}
class AuditInput {
  @IsUUID() call_id!: string;
  @IsIn(["call_started", "call_ended", "red_flag", "tool", "error"]) event_type!: string;
  @IsString() @MaxLength(2000) summary!: string;
}

@Controller()
export class BookingsController {
  constructor(private readonly service: BookingsService) {}

  private authorize(token: string | undefined): void {
    const expected = Buffer.from(process.env.CLINIC_API_TOKEN ?? "");
    const provided = Buffer.from(token ?? "");
    if (!expected.length || expected.length !== provided.length || !timingSafeEqual(expected, provided)) {
      throw new UnauthorizedException("Clinic API token required");
    }
  }

  @Get("health")
  health() { return this.service.health(); }

  @Get("bookings/available")
  available(@Query("date") date?: string, @Query("doctor") doctor?: string) {
    return this.service.available(date, doctor);
  }

  @Get("bookings")
  bookings() { return this.service.bookings(); }

  @Post("bookings/confirm")
  confirm(@Headers("x-clinic-token") token: string | undefined, @Body() body: BookingInput) {
    this.authorize(token);
    return this.service.bookSlot(body);
  }

  @Post("prescriptions/status")
  status(@Headers("x-clinic-token") token: string | undefined, @Body() body: IdentityInput) {
    this.authorize(token);
    return this.service.prescriptionStatus(body);
  }

  @Post("prescriptions/request")
  prescription(@Headers("x-clinic-token") token: string | undefined, @Body() body: PrescriptionInput) {
    this.authorize(token);
    return this.service.requestPrescription(body);
  }

  @Post("audit")
  audit(@Headers("x-clinic-token") token: string | undefined, @Body() body: AuditInput) {
    this.authorize(token);
    return this.service.audit(body.call_id, body.event_type, { summary: body.summary });
  }

  @Sse("events")
  events(): Observable<MessageEvent> { return this.service.events(); }
}
