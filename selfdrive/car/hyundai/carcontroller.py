from cereal import car, log
from common.numpy_fast import clip
from selfdrive.config import Conversions as CV
from selfdrive.car import apply_std_steer_torque_limits
from selfdrive.car.hyundai.spdcontroller  import SpdController
from selfdrive.car.hyundai.hyundaican import create_lkas11, create_clu11, \
                                             create_scc12, create_mdps12, create_AVM
from selfdrive.car.hyundai.values import Buttons, SteerLimitParams, LaneChangeParms, CAR
from opendbc.can.packer import CANPacker

from common.numpy_fast import interp
from common.params import Params

import copy


import common.log as trace1

VisualAlert = car.CarControl.HUDControl.VisualAlert
LaneChangeState = log.PathPlan.LaneChangeState


# Accel limits
ACCEL_HYST_GAP = 0.02  # don't change accel command for small oscilalitons within this value
ACCEL_MAX = 1.5  # 1.5 m/s2
ACCEL_MIN = -3.0 # 3   m/s2
ACCEL_SCALE = max(ACCEL_MAX, -ACCEL_MIN)


class CarController():
  def __init__(self, dbc_name, car_fingerprint):
    self.packer = CANPacker(dbc_name)
    self.car_fingerprint = car_fingerprint
    self.accel_steady = 0
    self.apply_steer_last = 0
    self.steer_rate_limited = False
    self.lkas11_cnt = 0
    self.scc12_cnt = 0
    self.resume_cnt = 0
    self.last_resume_frame = 0
    self.last_lead_distance = 0
    self.turning_signal_timer = 0
    self.lkas_button = 1

    self.longcontrol = 0 #TODO: make auto
    self.low_speed_car = False 
    self.streer_angle_over = False
    self.turning_indicator = 0 

    self.hud_timer_left = 0
    self.hud_timer_right = 0

    self.lkas_active_timer1 = 0
    self.lkas_active_timer2 = 0
    self.steer_timer = 0
    self.steer_torque_over_timer = 0
    self.steer_torque_over = False


    self.timer_curvature = 0
    self.SC = SpdController()
    self.sc_wait_timer2 = 0
    self.sc_active_timer2 = 0     
    self.sc_btn_type = Buttons.NONE
    self.sc_clu_speed = 0
    #self.model_speed = 255
    self.traceCC = trace1.Loger("CarCtrl")

    self.params = Params()
    self.lane_change_enabled = self.params.get('LaneChangeEnabled') == b'1'
    self.speed_control_enabled = self.params.get('SpeedControlEnabled') == b'1'



  def limit_ctrl(self, value, limit, offset ):
      p_limit = offset + limit
      m_limit = offset - limit
      if value > p_limit:
          value = p_limit
      elif  value < m_limit:
          value = m_limit
      return value

  def limit_dir(self, value, offset, p_l, m_l ):
      p_limit = offset + p_l
      m_limit = offset - m_l
      if value > p_limit:
          value = p_limit
      elif  value < m_limit:
          value = m_limit
      return value


  def accel_hysteresis(self, accel, accel_steady):
    # for small accel oscillations within ACCEL_HYST_GAP, don't change the accel command
    if accel > accel_steady + ACCEL_HYST_GAP:
      accel_steady = accel - ACCEL_HYST_GAP
    elif accel < accel_steady - ACCEL_HYST_GAP:
      accel_steady = accel + ACCEL_HYST_GAP
    accel = accel_steady
    return accel, accel_steady

  def process_hud_alert( self, enabled, button_on, visual_alert, left_line, right_line, CS ):
    hud_alert = 0
    if visual_alert == VisualAlert.steerRequired:
      hud_alert = 3

    # initialize to no line visible
    
    lane_visible = 1    # Lkas_LdwsSysState   LDWS
    if not button_on:
      lane_visible = 0
    elif left_line and right_line:   #or hud_alert:  #HUD alert only display when LKAS status is active
      if enabled:
        lane_visible = 3  # handle icon, lane icon
      else:
        lane_visible = 4   # lane icon
    elif left_line :
      lane_visible = 5      # left lan icon
    elif right_line:
      lane_visible = 6      # right lan icon

    if enabled and CS.Navi_HDA >= 1:  # highway Area
      if CS.v_ego > 40 * CV.KPH_TO_MS:
        lane_visible = 4

    # 7 : hud can't display,   panel :  LKA, handle icon. 
    return hud_alert, lane_visible



  def update(self, enabled, CS, frame, actuators, pcm_cancel_cmd, 
              visual_alert, left_line, right_line, sm, LaC ):

    path_plan = sm['pathPlan']
    # *** compute control surfaces ***
    v_ego_kph = CS.v_ego * CV.MS_TO_KPH

    # gas and brake
    apply_accel = actuators.gas - actuators.brake

    apply_accel, self.accel_steady = self.accel_hysteresis(apply_accel, self.accel_steady)
    apply_accel = clip(apply_accel * ACCEL_SCALE, ACCEL_MIN, ACCEL_MAX)
    abs_angle_steers =  abs(actuators.steerAngle) #  abs(CS.angle_steers)  # 

    param = SteerLimitParams()


    if path_plan.laneChangeState != LaneChangeState.off:
      #param.STEER_MAX *= 0.99
      param.STEER_DELTA_UP  = 2
      param.STEER_DELTA_DOWN = 5

    #v_curvature
    elif LaC.v_curvature < 200:
      self.timer_curvature = 300
    elif abs_angle_steers < 2 and  self.timer_curvature <= 0:
      xp = [0.5,1,1.5,2]
      fp = [240,245,250,255]
      param.STEER_MAX = interp( abs_angle_steers, xp, fp )

      if abs_angle_steers < 1 or v_ego_kph < 5:
          param.STEER_DELTA_UP  = 2
          param.STEER_DELTA_DOWN = 3



    if self.timer_curvature:
      self.timer_curvature -= 1

    ### Steering Torque
    new_steer = actuators.steer * param.STEER_MAX
    apply_steer = apply_std_steer_torque_limits(new_steer, self.apply_steer_last, CS.steer_torque_driver, param)
    self.steer_rate_limited = new_steer != apply_steer

    if self.car_fingerprint not in [CAR.GENESIS, CAR.SANTAFE]:
      if abs( CS.steer_torque_driver ) > 200: #180:
        self.steer_torque_over_timer += 1
        if self.steer_torque_over_timer > 5:
          self.steer_torque_over = True
          self.steer_torque_over_timer = 100
      elif self.steer_torque_over_timer:
        self.steer_torque_over_timer -= 1
      else:
        self.steer_torque_over = False
    else:
      self.steer_torque_over = False



    ### LKAS button to temporarily disable steering
    #if not CS.lkas_error:
    #  if self.lkas_button != CS.lkas_button_on:
    #     self.lkas_button = CS.lkas_button_on

    # disable if steer angle reach 90 deg, otherwise mdps fault in some models
    lkas_active = enabled and abs(CS.angle_steers) < 100.
    #lkas_active = enabled and abs(CS.angle_steers) < 100. and self.lkas_button

    low_speed = self.low_speed_car
    #if not self.lkas_button:
    #    low_speed = False
    #elif not CS.cruiseState.enabled:
    #    low_speed = False
    if CS.stopped:
        low_speed = False
    elif CS.v_ego > (CS.CP.minSteerSpeed + 0.7):
        low_speed = False
    elif CS.v_ego < (CS.CP.minSteerSpeed + 0.2):
        low_speed = True



    if self.low_speed_car != low_speed:
        self.low_speed_car = low_speed

    # streer over check
    if self.car_fingerprint not in [CAR.GENESIS, CAR.SANTAFE]:
      if enabled and abs(CS.angle_steers) > 100. or CS.steer_error:
        self.streer_angle_over =  True
        self.steer_timer = 250
      elif abs(CS.angle_steers) < 7.5 or not self.steer_timer:
        self.streer_angle_over =  False
      elif self.steer_timer:
        self.steer_timer -= 1
    else:
      self.streer_angle_over =  False

    # Disable steering while turning blinker on and speed below 60 kph
    if CS.left_blinker_on or CS.right_blinker_on:
      self.steer_torque_over = False
      self.turning_signal_timer = 100  # Disable for 1.0 Seconds after blinker turned off
    elif CS.left_blinker_flash or CS.right_blinker_flash:
      self.steer_torque_over = False
      self.turning_signal_timer = 100

    # turning indicator alert logic
    if self.lane_change_enabled:
      self.turning_indicator = self.turning_signal_timer and CS.v_ego <  LaneChangeParms.LANE_CHANGE_SPEED_MIN
    else:
      self.turning_indicator = self.turning_signal_timer

    if self.turning_signal_timer:
      self.turning_signal_timer -= 1 

    if left_line:
      self.hud_timer_left = 100
    elif self.hud_timer_left:
      self.hud_timer_left -= 1


    if right_line:
      self.hud_timer_right = 100      
    elif self.hud_timer_right:
      self.hud_timer_right -= 1


    if path_plan.laneChangeState != LaneChangeState.off:
      if LaC.v_curvature > 200:
        self.lkas_active_timer1 = 300
        apply_steer = self.limit_ctrl( apply_steer, 100, 0 )
    elif not self.hud_timer_left and  not self.hud_timer_right:
      self.lkas_active_timer1 = 200  #  apply_steer = 70
    elif path_plan.laneChangeState != LaneChangeState.off:
      self.steer_torque_over = False



    # disable lkas 
    if not CS.main_on:
      lkas_active = 0
      self.turning_signal_timer = 0
      self.turning_indicator = False
      self.steer_torque_over = False
    elif CS.stopped:
      lkas_active = 0
    elif self.steer_torque_over:
      lkas_active = 0

    if self.streer_angle_over:
      lkas_active = 0
    elif self.turning_indicator:
      lkas_active = 0


    if not lkas_active:
      apply_steer = 0

    steer_req = 1 if apply_steer else 0

    steer_limit = param.STEER_MAX
    if not lkas_active:
      self.lkas_active_timer1 = 200
    elif self.lkas_active_timer1 < 400: 
      self.lkas_active_timer1 += 1
      ratio_steer = self.lkas_active_timer1 / 400
      if ratio_steer < 1:
        steer_limit = ratio_steer * param.STEER_MAX
        apply_steer = self.limit_ctrl( apply_steer, steer_limit, 0 )



    dRel, yRel, vRel = self.SC.get_lead( sm, CS )
    vRel = int(vRel * 3.6 + 0.5)
  
    lead_objspd = CS.lead_objspd
    str_log1 = 'CURV={:03.0f}/{:06.3f} V_TORQ={:04.0f}'.format( LaC.v_curvature, LaC.model_sum, apply_steer )
    str_log2 = 'S_TORQ={:04.0f} ST_LIMIT={:03.0f} ST_MAX={:03.0f}'.format( CS.steer_torque_driver, steer_limit, param.STEER_MAX )
    trace1.printf( '{} {}'.format( str_log1, str_log2 ) )


    self.apply_accel_last = apply_accel
    self.apply_steer_last = apply_steer


    hud_alert, lane_visible = self.process_hud_alert(lkas_active, self.lkas_button, visual_alert, self.hud_timer_left, self.hud_timer_right, CS )    

    clu11_speed = CS.clu11["CF_Clu_Vanz"]
    enabled_speed = 38 if CS.is_set_speed_in_mph  else 60
    if clu11_speed > enabled_speed or not lkas_active:
      enabled_speed = clu11_speed

    can_sends = []

    if frame == 0: # initialize counts from last received count signals
      self.lkas11_cnt = CS.lkas11["CF_Lkas_MsgCount"] + 1
      self.scc12_cnt = CS.scc12["CR_VSM_Alive"] + 1 if not CS.no_radar else 0

    self.lkas11_cnt %= 0x10
    self.scc12_cnt %= 0xF
    self.clu11_cnt = frame % 0x10
    self.mdps12_cnt = frame % 0x100

    can_sends.append(create_lkas11(self.packer, self.car_fingerprint, 0, apply_steer, steer_req, self.lkas11_cnt, lkas_active,
                                   CS.lkas11, hud_alert, lane_visible, keep_stock=True))
    if CS.mdps_bus or CS.scc_bus == 1: # send lkas12 bus 1 if mdps or scc is on bus 1
      can_sends.append(create_lkas11(self.packer, self.car_fingerprint, 1, apply_steer, steer_req, self.lkas11_cnt, lkas_active,
                                   CS.lkas11, hud_alert, lane_visible, keep_stock=True))
    if CS.mdps_bus: # send clu11 to mdps if it is not on bus 0
      can_sends.append(create_clu11(self.packer, CS.mdps_bus, CS.clu11, Buttons.NONE, enabled_speed, self.clu11_cnt))

    if pcm_cancel_cmd and self.longcontrol:
      can_sends.append(create_clu11(self.packer, CS.scc_bus, CS.clu11, Buttons.CANCEL, clu11_speed, self.clu11_cnt))
    else: # send mdps12 to LKAS to prevent LKAS error if no cancel cmd
      can_sends.append(create_mdps12(self.packer, self.car_fingerprint, self.mdps12_cnt, CS.mdps12))

    if CS.scc_bus and self.longcontrol and frame % 2: # send scc12 to car if SCC not on bus 0 and longcontrol enabled
      can_sends.append(create_scc12(self.packer, apply_accel, enabled, self.scc12_cnt, CS.scc12))
      self.scc12_cnt += 1

    # AVM
    #if CS.mdps_bus:
    #if not CS.cp_AVM.can_valid:
    #can_sends.append(create_AVM(self.packer, self.car_fingerprint, CS.avm, CS ))
    

    if CS.stopped:
      #self.model_speed = 300
      # run only first time when the car stopped
      if self.last_lead_distance == 0:
        # get the lead distance from the Radar
        self.last_lead_distance = CS.lead_distance
        self.resume_cnt = 0
      # when lead car starts moving, create 6 RES msgs
      elif CS.lead_distance > self.last_lead_distance and (frame - self.last_resume_frame) > 5:
        can_sends.append(create_clu11(self.packer, CS.scc_bus, CS.clu11, Buttons.RES_ACCEL, clu11_speed, self.resume_cnt))
        self.resume_cnt += 1
        # interval after 6 msgs
        if self.resume_cnt > 5:
          self.last_resume_frame = frame
          self.resume_cnt = 0
    # reset lead distnce after the car starts moving
    elif self.last_lead_distance != 0:
      self.last_lead_distance = 0
    elif CS.driverOverride or not CS.pcm_acc_status or CS.clu_CruiseSwState == 1 or CS.clu_CruiseSwState == 2:
      #self.model_speed = 300
      self.resume_cnt = 0
      self.sc_btn_type = Buttons.NONE
      self.sc_wait_timer2 = 10
      self.sc_active_timer2 = 0
    elif self.sc_wait_timer2:
      self.sc_wait_timer2 -= 1
    elif self.speed_control_enabled:
      #acc_mode, clu_speed = self.long_speed_cntrl( v_ego_kph, CS, actuators )
      btn_type, clu_speed = self.SC.update( v_ego_kph, CS, sm, actuators, dRel, yRel, vRel, LaC.v_curvature )   # speed controller spdcontroller.py

      if CS.clu_Vanz < 5:
        self.sc_btn_type = Buttons.NONE
      elif self.sc_btn_type != Buttons.NONE:
        pass
      elif btn_type != Buttons.NONE:
        self.resume_cnt = 0
        self.sc_active_timer2 = 0
        self.sc_btn_type = btn_type
        self.sc_clu_speed = clu_speed

      if self.sc_btn_type != Buttons.NONE:
        self.sc_active_timer2 += 1
        if self.sc_active_timer2 > 10:
          self.sc_wait_timer2 = 5
          self.resume_cnt = 0
          self.sc_active_timer2 = 0
          self.sc_btn_type = Buttons.NONE          
        else:
          #self.traceCC.add( 'sc_btn_type={}  clu_speed={}  set={:.0f} vanz={:.0f}'.format( self.sc_btn_type, self.sc_clu_speed,  CS.VSetDis, clu11_speed  ) )
          can_sends.append(create_clu11(self.packer, CS.scc_bus, CS.clu11, self.sc_btn_type, self.sc_clu_speed, self.resume_cnt))
          self.resume_cnt += 1

    self.lkas11_cnt += 1

    return can_sends
