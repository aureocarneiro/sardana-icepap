##############################################################################
##
# This file is part of Sardana
##
# http://www.tango-controls.org/static/sardana/latest/doc/html/index.html
##
# Copyright 2011 CELLS / ALBA Synchrotron, Bellaterra, Spain
##
# Sardana is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
##
# Sardana is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
##
# You should have received a copy of the GNU Lesser General Public License
# along with Sardana.  If not, see <http://www.gnu.org/licenses/>.
##
##############################################################################
import time
import numpy
from sardana import State
from sardana.pool.pooldefs import SynchDomain, SynchParam
from sardana.pool.controller import TriggerGateController, Access, Memorize, \
    Memorized, Type, Description, DataAccess, DefaultValue
import taurus
import icepap

# [WIP] This controller need the Sardana PR 671 !!!!!

LOW = 'low'
HIGH = 'high'
ECAM = 'ecam'

MAX_ECAM_VALUES = 20477


class IcePAPTriggerController(TriggerGateController):
    """Basic IcePAPPositionTriggerGateController.
    """

    organization = "ALBA-Cells"
    gender = "TriggerGate"
    model = "Icepap"

    MaxDevice = 1

    ActivePeriod = 50e-6  # 50 micro seconds

    # The properties used to connect to the ICEPAP motor controller
    ctrl_properties = {
        'IcepapController': {
            Type: str,
            Description: 'Icepap Controller name'
        },
        'DefaultMotor': {
            Type: str,
            Description: 'motor base'
        },
        'UseMasterOut': {
            Type: bool,
            Description: 'use the master syncaux output',
            DefaultValue: True
        },
        'AxisInfos': {
            Type: str,
            Description: 'List of InfoX separated by colons, used '
                         'when the trigger is generated by the '
                         'axis UseMasterOut=False',
            DefaultValue: 'InfoA'
        },
        'Timeout': {
            Type: float,
            Description: 'Timeout used for the IcePAP socket communication',
            DefaultValue: 0.5
        }

    }
    axis_attributes = {
        # TODO: This attribute should be removed when the Sardana PR 671 is
        # integrated.
        'MasterMotor': {
            Type: str,
            Description: 'Master motor name used to generate the trigger',
            Access: DataAccess.ReadWrite,
            Memorize: Memorized
        },
        'StartTriggerOnly': {
            Type: bool,
            Description: 'Launch only the First trigger position',
            Access: DataAccess.ReadWrite,
            Memorize: Memorized
        },
    }

    def __init__(self, inst, props, *args, **kwargs):
        """
        :param inst:
        :param props:
        :param args:
        :param kwargs:
        :return:
        """
        TriggerGateController.__init__(self, inst, props, *args, **kwargs)
        self._log.debug('IcePAPTriggerCtr init....')

        self._time_mode = False
        self._start_trigger_only = False
        self._use_master_out = self.UseMasterOut
        self._axis_info_list = list(map(str.strip, self.AxisInfos.split(',')))

        # Calculate the number of retries according to the timeout and the
        # default Tango timeout (3s)
        self._retries_nr = 3 // (self.Timeout + 0.1)
        if self._retries_nr == 0:
            self._retries_nr = 1
        self._retries_nr = int(self._retries_nr)
        self._ipap_ctrl = taurus.Device(self.IcepapController)
        properties = self._ipap_ctrl.get_property(['host', 'port'])
        host = properties['host'][0]
        port = int(properties['port'][0])
        self._ipap = icepap.IcePAPController(host=host, port=port,
                                             timeout=self.Timeout,
                                             auto_axes=True)
        self._last_motor_name = None
        self._motor_axis = None
        self._motor_spu = 1
        self._motor_offset = 0
        self._motor_sign = 1

    def _set_out(self, out=LOW):
        motor = self._ipap[self._motor_axis]
        value = [out, 'normal']
        if self._use_master_out:
            motor.syncaux = value
        else:
            for info_out in self._axis_info_list:
                setattr(motor, info_out, value)
        print(motor.syncaux)

    def _configureMotor(self, motor_name):
        if motor_name is None:
            motor_name = self.DefaultMotor

        # TODO: Implement verification of the motor if it is part of the
        #  controller.

        self._last_motor_name = motor_name
        motor = taurus.Device(self._last_motor_name)
        self._motor_axis = int(motor.get_property('axis')['axis'][0])
        attrs = motor.read_attributes(['step_per_unit', 'offset', 'sign'])
        values = [attr.value for attr in attrs]
        self._motor_spu, self._motor_offset, self._motor_sign = values

        if motor_name == self._last_motor_name:
            return

        if self._use_master_out:
            # remove previous connection and connect the new motor
            pmux = self._ipap.get_pmux()
            for p in pmux:
                if 'E0' in p:
                    self._ipap.clear_pmux('e0')
                    break
            self._ipap.add_pmux(self._motor_axis, 'e0', pos=False, aux=True,
                                hard=True)

            pmux = self._ipap.get_pmux()
            self._log.debug('_connectMotor PMUX={0}'.format(pmux))

    def StateOne(self, axis):
        """Get the trigger/gate state"""
        # self._log.debug('StateOne(%d): entering...' % axis)
        hw_state = None
        for i in range(self._retries_nr):
            try:
                hw_state = self._ipap[self._motor_axis].state
                break
            except Exception:
                self._log.error('State reading error retry: {0}'.format(i))

        if hw_state is None or not hw_state.is_poweron():
            state = State.Alarm
            status = 'The motor is power off or not possible to read State'
        elif hw_state.is_moving() or hw_state.is_settling():
            state = State.Moving
            status = 'Moving'
        else:
            state = State.On
            status = 'Motor is not generating triggers.'

        return state, status

    def PreStartOne(self, axis, value=None):
        """PreStart the specified trigger"""
        # self._log.debug('PreStartOne(%d): entering...' % axis)
        if self._time_mode:
            self._set_out(out=LOW)
        else:
            self._set_out(out=ECAM)
        return True

    def StartOne(self, axis):
        """Overwrite the StartOne method"""
        if not self._time_mode:
            return
        self._set_out(out=HIGH)
        time.sleep(0.01)
        self._set_out(out=LOW)

    def AbortOne(self, axis):
        """Start the specified trigger"""
        self._log.debug('AbortOne(%d): entering...' % axis)
        self._set_out(out=LOW)

    def SetAxisPar(self, axis, name, value):
        idx = axis - 1
        tg = self.triggers[idx]
        name = name.lower()
        pars = ['offset', 'passive_interval', 'repetitions', 'sign',
                'info_channels']
        if name in pars:
            tg[name] = value

    def GetAxisPar(self, axis, name):
        idx = axis - 1
        tg = self.triggers[idx]
        name = name.lower()
        v = tg.get(name, None)
        if v is None:
            msg = ('GetAxisPar(%d). The parameter %s does not exist.'
                   % (axis, name))
            self._log.error(msg)
        return v

    def SynchOne(self, axis, configuration):
        # TODO: implement the configuration for multiples configuration
        synch_group = configuration[0]
        nr_points = synch_group[SynchParam.Repeats]

        if SynchParam.Initial not in synch_group:
            # Synchronization by time (step scan and ct)
            if nr_points > 1:
                msg = 'The IcePAP Trigger Controller is not allowed to ' \
                      'generate multiple trigger synchronized by time'
                raise ValueError(msg)
            else:
                self._time_mode = True

            if not self._use_master_out and \
                    self._last_motor_name != self.DefaultMotor:
                raise RuntimeError('The motor used in the scan is not the '
                                   'same than the motor configure with the '
                                   'trigger cable')
            self._configureMotor(self._last_motor_name)
            return

        self._time_mode = False
        # Synchronization by time and position (continuous scan)
        # TODO: Uncomment next line when Sardana PR 671 was integrated.
        # master = synch_group[SynchParam.Master][SynchDomain.Position]
        master = self._last_motor_name

        if not self._use_master_out and master != self.DefaultMotor:
            raise RuntimeError('The motor used in the scan is not the '
                               'same than the motor configure with the '
                               'trigger cable')

        self._configureMotor(master)

        start_user = synch_group[SynchParam.Initial][SynchDomain.Position]
        delta_user = synch_group[SynchParam.Total][SynchDomain.Position]

        start_user -= self._motor_offset
        start = start_user * self._motor_spu/self._motor_sign
        delta = delta_user * self._motor_spu/self._motor_sign

        end = start + delta * nr_points
        self._log.debug('IcepapTriggerCtr configuration: %f %f %d %d' %
                        (start, end, nr_points, delta))

        # There is a limitation of numbers of point on the icepap (20477)
        # ecamdat = motor.getAttribute('ecamdatinterval')
        # ecamdat.write([initial, final, nr_points], with_read=False)

        # The ecamdattable attribute is protected against non increasing
        # list at the icepap library level. HOWEVER, is not protected
        # agains list with repeated elements

        if self._start_trigger_only:
            trigger_table = numpy.array([start])
            self._log.debug('Start trigger only flag is active.')
        elif nr_points > MAX_ECAM_VALUES:
            msg = 'The Trigger by position not accept more than {0} ' \
                  'positions (points)'.format(MAX_ECAM_VALUES)
            raise RuntimeError(msg)
        else:
            trigger_table = numpy.linspace(start, end - delta,
                                           int(nr_points))
            self._log.debug('Table generated by numpy.linspace({0},{1},'
                            '{2}'.format(start, end-delta, nr_points))

        table_loaded = False
        for i in range(self._retries_nr):
            try:
                self._ipap[self._motor_axis].set_ecam_table(trigger_table)
                table_loaded = True
                break
            except Exception:
                self._log.warning('Send trigger table error retry: '
                                   '{0}'.format(i))
        if not table_loaded:
            raise RuntimeError('Can not send trigger table.')

    # -------------------------------------------------------------------------
    #               Axis Extra Parameters
    # -------------------------------------------------------------------------
    def setMasterMotor(self, axis, value):
        self._configureMotor(value)

    def getMasterMotor(self, axis):
        return self._last_motor_name

    def setStartTriggerOnly(self, axis, value):
        self._start_trigger_only = value

    def getStartTriggerOnly(self, axis):
        return self._start_trigger_only
