// 伪L2D 骨架工程（l2d_rig_core 自动生成，语义规范 = Adobe Character Animator）
(function(){
  var proj = app.project;
  while (proj.numItems > 0) { proj.item(1).remove(); }
  var MW = 1024, MH = 1024;
  var main = proj.items.addComp('l2d_rig', MW, MH, 1.0, 10.0, 30);
  main.bgColor = [0.10, 0.10, 0.12];

  // ---- body (父 root) ----
  var c_body = proj.items.addComp('node_body', MW, MH, 1.0, 10.0, 30);
  var f_v_neck = proj.items.importItem(new File('neck'));
  try { f_v_neck.mainSource.colorPremultiplied = false; } catch(e){}
  var l_v_neck = c_body.layers.add(f_v_neck); l_v_neck.name='neck';
  var f_v_topwear = proj.items.importItem(new File('topwear'));
  try { f_v_topwear.mainSource.colorPremultiplied = false; } catch(e){}
  var l_v_topwear = c_body.layers.add(f_v_topwear); l_v_topwear.name='topwear';
  var h_body = c_body.layers.add(c_body); h_body.name='body_holder';
  h_body.property('Anchor Point').setValue([587.5, 1023.0]);
  h_body.property('Position').setValue([MW/2, MH/2]);

  // ---- brow_left (父 head) ----
  var c_brow_left = proj.items.addComp('node_brow_left', MW, MH, 1.0, 10.0, 30);
  var f_v_eyebrow_l = proj.items.importItem(new File('eyebrow-l'));
  try { f_v_eyebrow_l.mainSource.colorPremultiplied = false; } catch(e){}
  var l_v_eyebrow_l = c_brow_left.layers.add(f_v_eyebrow_l); l_v_eyebrow_l.name='eyebrow-l';
  var h_brow_left = c_brow_left.layers.add(c_brow_left); h_brow_left.name='brow_left_holder';
  h_brow_left.property('Anchor Point').setValue([518.5, 276.5]);
  h_brow_left.property('Position').setValue([MW/2, MH/2]);

  // ---- brow_right (父 head) ----
  var c_brow_right = proj.items.addComp('node_brow_right', MW, MH, 1.0, 10.0, 30);
  var f_v_eyebrow_r = proj.items.importItem(new File('eyebrow-r'));
  try { f_v_eyebrow_r.mainSource.colorPremultiplied = false; } catch(e){}
  var l_v_eyebrow_r = c_brow_right.layers.add(f_v_eyebrow_r); l_v_eyebrow_r.name='eyebrow-r';
  var h_brow_right = c_brow_right.layers.add(c_brow_right); h_brow_right.name='brow_right_holder';
  h_brow_right.property('Anchor Point').setValue([396.0, 232.0]);
  h_brow_right.property('Position').setValue([MW/2, MH/2]);

  // ---- eye_left (父 head) ----
  var c_eye_left = proj.items.addComp('node_eye_left', MW, MH, 1.0, 10.0, 30);
  var f_v_eyewhite_l = proj.items.importItem(new File('eyewhite-l'));
  try { f_v_eyewhite_l.mainSource.colorPremultiplied = false; } catch(e){}
  var l_v_eyewhite_l = c_eye_left.layers.add(f_v_eyewhite_l); l_v_eyewhite_l.name='eyewhite-l';
  var f_v_irides_l = proj.items.importItem(new File('irides-l'));
  try { f_v_irides_l.mainSource.colorPremultiplied = false; } catch(e){}
  var l_v_irides_l = c_eye_left.layers.add(f_v_irides_l); l_v_irides_l.name='irides-l';
  var h_eye_left = c_eye_left.layers.add(c_eye_left); h_eye_left.name='eye_left_holder';
  h_eye_left.property('Anchor Point').setValue([526.0, 381.0]);
  h_eye_left.property('Position').setValue([MW/2, MH/2]);

  // ---- eye_right (父 head) ----
  var c_eye_right = proj.items.addComp('node_eye_right', MW, MH, 1.0, 10.0, 30);
  var f_v_eyewhite_r = proj.items.importItem(new File('eyewhite-r'));
  try { f_v_eyewhite_r.mainSource.colorPremultiplied = false; } catch(e){}
  var l_v_eyewhite_r = c_eye_right.layers.add(f_v_eyewhite_r); l_v_eyewhite_r.name='eyewhite-r';
  var f_v_irides_r = proj.items.importItem(new File('irides-r'));
  try { f_v_irides_r.mainSource.colorPremultiplied = false; } catch(e){}
  var l_v_irides_r = c_eye_right.layers.add(f_v_irides_r); l_v_irides_r.name='irides-r';
  var h_eye_right = c_eye_right.layers.add(c_eye_right); h_eye_right.name='eye_right_holder';
  h_eye_right.property('Anchor Point').setValue([397.5, 331.0]);
  h_eye_right.property('Position').setValue([MW/2, MH/2]);

  // ---- hair_back (父 head) ----
  var c_hair_back = proj.items.addComp('node_hair_back', MW, MH, 1.0, 10.0, 30);
  var f_v_back_hair = proj.items.importItem(new File('back hair'));
  try { f_v_back_hair.mainSource.colorPremultiplied = false; } catch(e){}
  var l_v_back_hair = c_hair_back.layers.add(f_v_back_hair); l_v_back_hair.name='back hair';
  var h_hair_back = c_hair_back.layers.add(c_hair_back); h_hair_back.name='hair_back_holder';
  h_hair_back.property('Anchor Point').setValue([521.0, 74.0]);
  h_hair_back.property('Position').setValue([MW/2, MH/2]);

  // ---- hair_front (父 head) ----
  var c_hair_front = proj.items.addComp('node_hair_front', MW, MH, 1.0, 10.0, 30);
  var f_v_front_hair = proj.items.importItem(new File('front hair'));
  try { f_v_front_hair.mainSource.colorPremultiplied = false; } catch(e){}
  var l_v_front_hair = c_hair_front.layers.add(f_v_front_hair); l_v_front_hair.name='front hair';
  var h_hair_front = c_hair_front.layers.add(c_hair_front); h_hair_front.name='hair_front_holder';
  h_hair_front.property('Anchor Point').setValue([521.0, 74.0]);
  h_hair_front.property('Position').setValue([MW/2, MH/2]);

  // ---- head (父 body) ----
  var c_head = proj.items.addComp('node_head', MW, MH, 1.0, 10.0, 30);
  var f_v_ears_r = proj.items.importItem(new File('ears-r'));
  try { f_v_ears_r.mainSource.colorPremultiplied = false; } catch(e){}
  var l_v_ears_r = c_head.layers.add(f_v_ears_r); l_v_ears_r.name='ears-r';
  var f_v_face = proj.items.importItem(new File('face'));
  try { f_v_face.mainSource.colorPremultiplied = false; } catch(e){}
  var l_v_face = c_head.layers.add(f_v_face); l_v_face.name='face';
  var f_v_mouth = proj.items.importItem(new File('mouth'));
  try { f_v_mouth.mainSource.colorPremultiplied = false; } catch(e){}
  var l_v_mouth = c_head.layers.add(f_v_mouth); l_v_mouth.name='mouth';
  var f_v_ears_l = proj.items.importItem(new File('ears-l'));
  try { f_v_ears_l.mainSource.colorPremultiplied = false; } catch(e){}
  var l_v_ears_l = c_head.layers.add(f_v_ears_l); l_v_ears_l.name='ears-l';
  var f_v_nose = proj.items.importItem(new File('nose'));
  try { f_v_nose.mainSource.colorPremultiplied = false; } catch(e){}
  var l_v_nose = c_head.layers.add(f_v_nose); l_v_nose.name='nose';
  var h_head = c_head.layers.add(c_head); h_head.name='head_holder';
  h_head.property('Anchor Point').setValue([525.0, 413.0]);
  h_head.property('Position').setValue([MW/2, MH/2]);

  // ---- lid_left (父 head) ----
  var c_lid_left = proj.items.addComp('node_lid_left', MW, MH, 1.0, 10.0, 30);
  var f_v_eyelash_l = proj.items.importItem(new File('eyelash-l'));
  try { f_v_eyelash_l.mainSource.colorPremultiplied = false; } catch(e){}
  var l_v_eyelash_l = c_lid_left.layers.add(f_v_eyelash_l); l_v_eyelash_l.name='eyelash-l';
  var h_lid_left = c_lid_left.layers.add(c_lid_left); h_lid_left.name='lid_left_holder';
  h_lid_left.property('Anchor Point').setValue([528.0, 388.0]);
  h_lid_left.property('Position').setValue([MW/2, MH/2]);

  // ---- lid_right (父 head) ----
  var c_lid_right = proj.items.addComp('node_lid_right', MW, MH, 1.0, 10.0, 30);
  var f_v_eyelash_r = proj.items.importItem(new File('eyelash-r'));
  try { f_v_eyelash_r.mainSource.colorPremultiplied = false; } catch(e){}
  var l_v_eyelash_r = c_lid_right.layers.add(f_v_eyelash_r); l_v_eyelash_r.name='eyelash-r';
  var h_lid_right = c_lid_right.layers.add(c_lid_right); h_lid_right.name='lid_right_holder';
  h_lid_right.property('Anchor Point').setValue([390.5, 331.0]);
  h_lid_right.property('Position').setValue([MW/2, MH/2]);

  var L_body = main.layers.add(c_body); L_body.name='body';
  var L_brow_left = c_head.layers.add(c_brow_left); L_brow_left.name='brow_left';
  var L_brow_right = c_head.layers.add(c_brow_right); L_brow_right.name='brow_right';
  var L_eye_left = c_head.layers.add(c_eye_left); L_eye_left.name='eye_left';
  var L_eye_right = c_head.layers.add(c_eye_right); L_eye_right.name='eye_right';
  var L_hair_back = c_head.layers.add(c_hair_back); L_hair_back.name='hair_back';
  var L_hair_front = c_head.layers.add(c_hair_front); L_hair_front.name='hair_front';
  var L_head = c_body.layers.add(c_head); L_head.name='head';
  var L_lid_left = c_head.layers.add(c_lid_left); L_lid_left.name='lid_left';
  var L_lid_right = c_head.layers.add(c_lid_right); L_lid_right.name='lid_right';

  L_body.property('Position').expression = '[value[0], value[1] + 3*Math.sin(time*2.1)]';
  L_eye_left.property('Scale').expression = 'var p=4.6,d=0.16,ph=time%p,s=1;if(ph<d){s=1-0.94*(ph/d);} else if(ph<2*d){s=0.06+0.94*((ph-d)/d);} [value[0], s*value[1]/100];';
  L_eye_right.property('Scale').expression = 'var p=4.6,d=0.16,ph=time%p,s=1;if(ph<d){s=1-0.94*(ph/d);} else if(ph<2*d){s=0.06+0.94*((ph-d)/d);} [value[0], s*value[1]/100];';
  L_hair_back.property('Rotation').expression = 'wiggle(1.1, 2.4)[0]';
  L_hair_front.property('Rotation').expression = 'wiggle(1.5, 3.0)[0]';
  L_head.property('Rotation').expression = 'wiggle(0.9, 1.6)[0]';
  L_lid_left.property('Scale').expression = 'var p=4.6,d=0.16,ph=time%p,s=1;if(ph<d){s=1-0.94*(ph/d);} else if(ph<2*d){s=0.06+0.94*((ph-d)/d);} [value[0], s*value[1]/100];';
  L_lid_right.property('Scale').expression = 'var p=4.6,d=0.16,ph=time%p,s=1;if(ph<d){s=1-0.94*(ph/d);} else if(ph<2*d){s=0.06+0.94*((ph-d)/d);} [value[0], s*value[1]/100];';
  proj.saveAs(new File('D:/work/puppet/muppet_studio/示例工程/rig_build.jsx'));
})();